// controllers/flavourrouter_controller.go
package controller

import (
	"context"
	"fmt"
	"reflect"

	//"strconv"
	"time"

	//appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"

	networkingapi "istio.io/api/networking/v1alpha3"
	networkingkube "istio.io/client-go/pkg/apis/networking/v1alpha3"

	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/builder"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/event"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	"sigs.k8s.io/controller-runtime/pkg/predicate"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	schedulingv1alpha1 "github.com/belgio99/k8s-carbonshift/operator/api/v1alpha1"
)

/* ─────────────────────────────────────────  Costanti  ───────────────────────────────────────── */
const (
	carbonLabel            = "carbonshift"                   // high|mid|low
	enableLabel            = "carbonshift/enabled"           // opt-in
	origReplicasAnnotation = "carbonshift/original-replicas" // remember replicas
	defaultRequeue         = 30 * time.Second
)

var (
	flavors        = []string{"high", "mid", "low"}
	queueSvcSuffix = "-queue" // nome Service della coda <svc>-queue
)

/* ─────────────────────────────────────── Reconciler  ────────────────────────────────────────── */

type FlavourRouterReconciler struct {
	client.Client
	Scheme *runtime.Scheme
}

/* -------------------------- RBAC -------------------------- */

// +kubebuilder:rbac:groups=core,resources=services,verbs=get;list;watch;update;patch
// +kubebuilder:rbac:groups=apps,resources=deployments,verbs=get;list;watch;update;patch
// +kubebuilder:rbac:groups=scheduling.carbonshift.io,resources=trafficschedules,verbs=get;list;watch
// +kubebuilder:rbac:groups=networking.istio.io,resources=virtualservices;destinationrules,verbs=get;list;watch;create;update;patch;delete

/* -------------------------- Reconcile -------------------------- */

func (r *FlavourRouterReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	log := ctrl.LoggerFrom(ctx).WithValues("service", req.NamespacedName)

	// 1. Service opt-in
	// Gets the service that has the label "carbonshift/enabled=true", which is our "target" service.
	var svc corev1.Service
	if err := r.Get(ctx, req.NamespacedName, &svc); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}
	if svc.Labels[enableLabel] != "true" {
		return ctrl.Result{}, nil
	}

	// 2. Get the TrafficSchedule CR from the cluster
	var tsList schedulingv1alpha1.TrafficScheduleList
	if err := r.List(ctx, &tsList); err != nil {
		return ctrl.Result{}, err
	}
	if len(tsList.Items) == 0 {
		log.Info("No TrafficSchedule – requeue") //if no TrafficSchedule is found, requeue
		return ctrl.Result{RequeueAfter: defaultRequeue}, nil
	}
	trafficschedule := tsList.Items[0].Status

	// 3. Get the weights for flavours and direct/queue
	weightsFlavour := map[string]int{"high": 0, "mid": 0, "low": 0}
	for _, fr := range trafficschedule.FlavorRules {
		weightsFlavour[fr.FlavorName] = fr.Weight
		log.Info("Flavor weights", "flavor", fr.FlavorName, "weight", fr.Weight)
	}
	directW := trafficschedule.DirectWeight
	queueW := trafficschedule.QueueWeight

	// 4. Create or update the Gateway, DestinationRule and VirtualServices

	if err := r.ensureGateway(ctx, svc); err != nil {
		return ctrl.Result{}, err
	}
	if err := r.ensureDR(ctx, &svc); err != nil {
		return ctrl.Result{}, err
	}
	if err := r.ensureEntryVS(ctx, &svc, directW, queueW); err != nil {
		return ctrl.Result{}, err
	}
	if err := r.ensureFlavourVS(ctx, &svc, directW, weightsFlavour); err != nil {
		return ctrl.Result{}, err
	}

	/* 5. Autoscaling a zero
	   /*if err := r.handleScaling(ctx, svc.Namespace, weightsFlavour); err != nil {
	       return ctrl.Result{}, err
	   }*/

	// 6. Re-queue in base a ValidUntil
	if !trafficschedule.ValidUntil.IsZero() {
		delay := time.Until(trafficschedule.ValidUntil.Time)
		if delay < 0 {
			delay = 0
		}
		log.Info("[FlavourRouter]: Requeuing for next TrafficSchedule", "validUntil", trafficschedule.ValidUntil.Time, "delay", delay)
		return ctrl.Result{RequeueAfter: delay}, nil
	}
	return ctrl.Result{}, nil
}


func (r *FlavourRouterReconciler) ensureGateway(ctx context.Context,svc corev1.Service) error {

	ctrl.LoggerFrom(ctx).Info("Ensuring Gateway for service", "service", svc.Name)
	name := fmt.Sprintf("%s-entry-gw", svc.Name)

	if len(svc.Spec.Ports) == 0 {
		return fmt.Errorf("service %s/%s has no ports defined", svc.Namespace, svc.Name)
	}
	p := svc.Spec.Ports[0] // prendiamo la prima porta
	proto := "HTTP"
	if p.Port == 443 || p.Name == "https" {
		proto = "HTTPS"
	}

	host := fmt.Sprintf("%s.%s.svc.cluster.local", svc.Name, svc.Namespace)

	gw := networkingkube.Gateway{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: svc.Namespace},
		Spec: networkingapi.Gateway{
			Selector: map[string]string{"istio": "ingressgateway"}, // use the default Istio ingress gateway
			Servers: []*networkingapi.Server{{
				Port: &networkingapi.Port{
					Number:   uint32(p.Port),
					Name:     p.Name,
					Protocol: proto,
				},
				Hosts: []string{host},
			}},
		},
	}

	if err := ctrl.SetControllerReference(&svc, &gw, r.Scheme); err != nil {
		return err
	}

	var cur networkingkube.Gateway
	err := r.Get(ctx, client.ObjectKey{Namespace: svc.Namespace, Name: name}, &cur)
	switch {
	case apierrors.IsNotFound(err):
		return r.Create(ctx, &gw)
	case err != nil:
		return err
	case !reflect.DeepEqual(cur.Spec, gw.Spec):
		cur.Spec = gw.Spec
		ctrl.LoggerFrom(ctx).Info("Gateway updated", "name", name, "namespace", svc.Namespace)
		return r.Update(ctx, &cur)
	}
	return nil
}

func (r *FlavourRouterReconciler) ensureDR(ctx context.Context, svc *corev1.Service) error {
	ctrl.LoggerFrom(ctx).Info("Ensuring DestinationRule for service", "service", svc.Name)
	name := fmt.Sprintf("%s-carbonshift-dr", svc.Name)

	newDR := networkingkube.DestinationRule{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: svc.Namespace},
		Spec: networkingapi.DestinationRule{
			Host: svc.Name,
			Subsets: []*networkingapi.Subset{
				{Name: "high", Labels: map[string]string{carbonLabel: "high"}},
				{Name: "mid", Labels: map[string]string{carbonLabel: "mid"}},
				{Name: "low", Labels: map[string]string{carbonLabel: "low"}},
			},
		},
	}
	if err := ctrl.SetControllerReference(svc, &newDR, r.Scheme); err != nil {
		return err
	}

	var currentDR networkingkube.DestinationRule
	err := r.Get(ctx, client.ObjectKey{Namespace: svc.Namespace, Name: name}, &currentDR)
	switch {
	case apierrors.IsNotFound(err):
		return r.Create(ctx, &newDR)
	case err != nil:
		return err
	case !reflect.DeepEqual(currentDR.Spec, newDR.Spec): // Update the DestinationRule if it differs
		currentDR.Spec = newDR.Spec
		ctrl.LoggerFrom(ctx).Info("DestinationRule was updated", "name", name, "namespace", svc.Namespace)
		return r.Update(ctx, &currentDR)
	}
	return nil
}

func (r *FlavourRouterReconciler) ensureEntryVS(ctx context.Context, svc *corev1.Service, directW, queueW int) error {
	ctrl.LoggerFrom(ctx).Info("Ensuring Entry VirtualService for service", "service", svc.Name)
	name := fmt.Sprintf("%s-entry-vs", svc.Name)
	host := fmt.Sprintf("%s.%s.svc.cluster.local", svc.Name, svc.Namespace)
	queueHost := fmt.Sprintf("%s%s.%s.svc.cluster.local", svc.Name, queueSvcSuffix, svc.Namespace)

	vs := networkingkube.VirtualService{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: svc.Namespace},
		Spec: networkingapi.VirtualService{
			Hosts:    []string{host},
			Gateways: []string{fmt.Sprintf("%s/%s-entry-gw", svc.Namespace, svc.Name)},
			Http: []*networkingapi.HTTPRoute{
				{
					Match: []*networkingapi.HTTPMatchRequest{{
						Headers: map[string]*networkingapi.StringMatch{
							"X-Urgent": {MatchType: &networkingapi.StringMatch_Exact{Exact: "true"}},
						},
					}},
					Route: []*networkingapi.HTTPRouteDestination{{
						Destination: &networkingapi.Destination{Host: host},
						Weight:      100,
					}},
				},
				{
					Route: []*networkingapi.HTTPRouteDestination{
						{
							Destination: &networkingapi.Destination{Host: host}, // Direct traffic to the service
							Weight:      int32(directW),
						},
						{
							Destination: &networkingapi.Destination{Host: queueHost}, // Traffic sent to the queue service
							Weight:      int32(queueW),
						},
					},
				},
			},
		},
	}
	if err := ctrl.SetControllerReference(svc, &vs, r.Scheme); err != nil {
		return err
	}

	var cur networkingkube.VirtualService
	err := r.Get(ctx, client.ObjectKey{Namespace: svc.Namespace, Name: name}, &cur)
	switch {
	case apierrors.IsNotFound(err):
		return r.Create(ctx, &vs)
	case err != nil:
		return err
	case !reflect.DeepEqual(cur.Spec, vs.Spec):
		cur.Spec = vs.Spec
		ctrl.LoggerFrom(ctx).Info("Entry VirtualService was updated", "name", name, "namespace", svc.Namespace)
		return r.Update(ctx, &cur) // Update the Entry VirtualService if it differs
	}
	return nil
}

func (r *FlavourRouterReconciler) ensureFlavourVS(ctx context.Context, svc *corev1.Service, directW int, wFl map[string]int) error {
	name := fmt.Sprintf("%s-flavour-vs", svc.Name)
	host := fmt.Sprintf("%s.%s.svc.cluster.local", svc.Name, svc.Namespace)
	ctrl.LoggerFrom(ctx).Info("Ensuring Flavour VirtualService for service", "service", svc.Name)

	highW := int32(wFl["high-power"])
	midW := int32(wFl["mid-power"])
	lowW := int32(wFl["low-power"])

	matches := []struct{ val, subset string }{
		{"high", "high"}, {"mid", "mid"}, {"low", "low"},
	}

	var httpRoutes []*networkingapi.HTTPRoute
	// Traffic "forced" to go to a specific flavour
	for _, m := range matches {
		httpRoutes = append(httpRoutes, &networkingapi.HTTPRoute{
			Match: []*networkingapi.HTTPMatchRequest{{
				Headers: map[string]*networkingapi.StringMatch{
					"X-Carbonshift": {MatchType: &networkingapi.StringMatch_Exact{Exact: m.val}},
				},
			}},
			Route: []*networkingapi.HTTPRouteDestination{{
				Destination: &networkingapi.Destination{Host: host, Subset: m.subset},
				Weight:      100,
			}},
		})
	}

	// "Normal" traffic routing to the flavours
	httpRoutes = append(httpRoutes, &networkingapi.HTTPRoute{
		Route: []*networkingapi.HTTPRouteDestination{
			{Destination: &networkingapi.Destination{Host: host, Subset: "high"}, Weight: highW},
			{Destination: &networkingapi.Destination{Host: host, Subset: "mid"}, Weight: midW},
			{Destination: &networkingapi.Destination{Host: host, Subset: "low"}, Weight: lowW},
		},
	})

	vs := networkingkube.VirtualService{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: svc.Namespace},
		Spec: networkingapi.VirtualService{
			Hosts: []string{host},
			Http:  httpRoutes,
		},
	}

	if err := ctrl.SetControllerReference(svc, &vs, r.Scheme); err != nil {
		return err
	}

	var cur networkingkube.VirtualService
	err := r.Get(ctx, client.ObjectKey{Namespace: svc.Namespace, Name: name}, &cur)
	switch {
	case apierrors.IsNotFound(err):
		return r.Create(ctx, &vs)
	case err != nil:
		return err
	case !reflect.DeepEqual(cur.Spec, vs.Spec):
		cur.Spec = vs.Spec
		ctrl.LoggerFrom(ctx).Info("Flavour VirtualService was updated", "name", name, "namespace", svc.Namespace)
		return r.Update(ctx, &cur)
	}
	return nil
}

/*
func (r *FlavourRouterReconciler) handleScaling(ctx context.Context, ns string, weights map[string]int) error {
    for _, fl := range flavors {
        var depList appsv1.DeploymentList
        if err := r.List(ctx, &depList,
            client.InNamespace(ns),
            client.MatchingLabels{carbonLabel: fl}); err != nil {
            return err
        }
        for i := range depList.Items {
            dep := &depList.Items[i]
            wantZero := weights[fl] == 0

            replicas := int32(1)
            if dep.Spec.Replicas != nil {
                replicas = *dep.Spec.Replicas
            }

            switch {
            case wantZero && replicas != 0:
                if dep.Annotations == nil { dep.Annotations = map[string]string{} }
                dep.Annotations[origReplicasAnnotation] = strconv.Itoa(int(replicas))
                z := int32(0); dep.Spec.Replicas = &z
                if err := r.Update(ctx, dep); err != nil { return err }

            case !wantZero && replicas == 0:
                restore := int32(1)
                if s, ok := dep.Annotations[origReplicasAnnotation]; ok {
                    if n, err := strconv.Atoi(s); err == nil { restore = int32(n) }
                }
                dep.Spec.Replicas = &restore
                if err := r.Update(ctx, dep); err != nil { return err }
            }
        }
    }
    return nil
}
*/

func (r *FlavourRouterReconciler) SetupWithManager(mgr ctrl.Manager) error {

	svcPred := predicate.Funcs{
		CreateFunc: func(e event.CreateEvent) bool {
			return e.Object.GetLabels()[enableLabel] == "true"
		},
		UpdateFunc: func(e event.UpdateEvent) bool {
			return e.ObjectNew.GetLabels()[enableLabel] == "true"
		},
		DeleteFunc: func(event.DeleteEvent) bool { return false },
	}

	mapTS := handler.EnqueueRequestsFromMapFunc(func(ctx context.Context, obj client.Object) []reconcile.Request {
		var list corev1.ServiceList
		if err := mgr.GetClient().List(ctx, &list); err != nil {
			return nil
		}
		var out []reconcile.Request
		for _, s := range list.Items {
			if s.Labels[enableLabel] == "true" {
				out = append(out, reconcile.Request{NamespacedName: client.ObjectKeyFromObject(&s)})
			}
		}
		return out
	})

	return ctrl.NewControllerManagedBy(mgr).
		For(&corev1.Service{}, builder.WithPredicates(svcPred)).
		Watches(&schedulingv1alpha1.TrafficSchedule{}, mapTS).
		Complete(r)
}
