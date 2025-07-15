// controllers/flavourrouter_controller.go
package controller

import (
	"context"
	"fmt"

	//"strconv"
	"time"

	//appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/apimachinery/pkg/api/equality"
	"k8s.io/client-go/util/retry"
	"k8s.io/utils/ptr"

	kedav1alpha1 "github.com/kedacore/keda/v2/apis/keda/v1alpha1"
	networkingapi "istio.io/api/networking/v1alpha3"
	networkingkube "istio.io/client-go/pkg/apis/networking/v1alpha3"
	appsv1 "k8s.io/api/apps/v1"
	rbacv1 "k8s.io/api/rbac/v1"

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
	flavours       = []string{"high-power", "mid-power", "low-power"}
	queueSvcSuffix = "-queue" // nome Service della coda <svc>-queue
)

/* ─────────────────────────────────────── Reconciler  ────────────────────────────────────────── */

type FlavourRouterReconciler struct {
	client.Client
	Scheme *runtime.Scheme
}

/* -------------------------- RBAC -------------------------- */

// +kubebuilder:rbac:groups=core,resources=services,verbs=get;list;watch;create;update;patch
// +kubebuilder:rbac:groups=apps,resources=deployments,verbs=get;list;watch;create;update;patch
// +kubebuilder:rbac:groups=scheduling.carbonshift.io,resources=trafficschedules,verbs=get;list;watch
// +kubebuilder:rbac:groups=networking.istio.io,resources=virtualservices;destinationrules,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=keda.sh,resources=scaledobjects,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups="",resources=serviceaccounts,verbs=get;list;watch;create;update;patch
// +kubebuilder:rbac:groups=rbac.authorization.k8s.io,resources=clusterrolebindings,verbs=get;list;watch;create;update;patch

/* -------------------------- Reconcile -------------------------- */

func (r *FlavourRouterReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	log := ctrl.LoggerFrom(ctx).WithName("[FlavourRouter]").WithValues("service", req.NamespacedName)

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
	tsSpec := tsList.Items[0].Spec
	trafficschedule := tsList.Items[0].Status

	// 3. Get the weights for flavours and direct/queue
	weightsFlavour := map[string]int{"high-power": 0, "mid-power": 0, "low-power": 0}
	for _, fr := range trafficschedule.FlavourRules {
		weightsFlavour[fr.FlavourName] = fr.Weight
		log.Info("Flavour weights", "flavour", fr.FlavourName, "weight", fr.Weight)
	}
	directW := trafficschedule.DirectWeight

	// 4. Create or update all necessary resources
	if err := r.ensureServiceAccount(ctx, &svc); err != nil {
		return ctrl.Result{}, err
	}

	if err := r.ensureClusterRoleBinding(ctx, &svc); err != nil {
		return ctrl.Result{}, err
	}

	if err := r.ensureBufferServiceDeployment(ctx, &svc, "router", tsSpec.Router.Resources); err != nil {
		return ctrl.Result{}, err
	}

	if err := r.ensureBufferServiceService(ctx, &svc, "router"); err != nil {
		return ctrl.Result{}, err
	}

	if err := r.ensureBufferServiceDeployment(ctx, &svc, "consumer", tsSpec.Consumer.Resources); err != nil {
		return ctrl.Result{}, err
	}

	if err := r.ensureBufferServiceService(ctx, &svc, "consumer"); err != nil {
		return ctrl.Result{}, err
	}

	if err := r.ensureConsumerScaledObject(ctx, &svc, tsSpec.Consumer.Autoscaling); err != nil {
		return ctrl.Result{}, err
	}

	for _, flavour := range flavours {
		if err := r.ensureFlavourScaledObject(ctx, &svc, flavour, tsSpec.Target.Autoscaling); err != nil {
			return ctrl.Result{}, err
		}
	}

	if err := r.ensureDR(ctx, &svc); err != nil {
		return ctrl.Result{}, err
	}

	if err := r.ensureFlavourVS(ctx, &svc, directW, weightsFlavour); err != nil {
		return ctrl.Result{}, err
	}

	// 5. Re-queue in base a ValidUntil
	if !trafficschedule.ValidUntil.IsZero() {
		delay := time.Until(trafficschedule.ValidUntil.Time)
		if delay < 0 {
			delay = 0
		}
		log.Info("Requeuing for next TrafficSchedule", "validUntil", trafficschedule.ValidUntil.Time, "delay", delay)
		return ctrl.Result{RequeueAfter: delay}, nil
	}
	return ctrl.Result{}, nil
}

func (r *FlavourRouterReconciler) ensureDR(ctx context.Context, svc *corev1.Service) error {
	log := ctrl.LoggerFrom(ctx).WithName("[FlavourRouter]")
	log.Info("Ensuring DestinationRule for service", "service", svc.Name)
	name := fmt.Sprintf("%s-carbonshift-dr", svc.Name)
	host := fmt.Sprintf("%s.%s.svc.cluster.local", svc.Name, svc.Namespace)

	newDR := networkingkube.DestinationRule{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: svc.Namespace},
		Spec: networkingapi.DestinationRule{
			Host: host,
			Subsets: []*networkingapi.Subset{
				{Name: "high-power", Labels: map[string]string{carbonLabel: "high"}},
				{Name: "mid-power", Labels: map[string]string{carbonLabel: "mid"}},
				{Name: "low-power", Labels: map[string]string{carbonLabel: "low"}},
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
	case !equality.Semantic.DeepEqual(currentDR.Spec, newDR.Spec): // Update the DestinationRule if it differs
		currentDR.Spec = newDR.Spec
		log.Info("DestinationRule was updated", "name", name, "namespace", svc.Namespace)
		return r.Update(ctx, &currentDR)
	}
	return nil
}

func (r *FlavourRouterReconciler) ensureEntryVS(ctx context.Context, svc *corev1.Service, directW, queueW int) error {
	log := ctrl.LoggerFrom(ctx).WithName("[FlavourRouter]")
	log.Info("Ensuring Entry VirtualService for service", "service", svc.Name)
	name := fmt.Sprintf("%s-entry-vs", svc.Name)
	host := fmt.Sprintf("%s-flavour-vs.%s.svc.cluster.local", svc.Name, svc.Namespace)
	sourceHost := fmt.Sprintf("%s.example.com", svc.Namespace)
	queueHost := fmt.Sprintf("%s%s.%s.svc.cluster.local", svc.Name, queueSvcSuffix, svc.Namespace)

	vs := networkingkube.VirtualService{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: svc.Namespace},
		Spec: networkingapi.VirtualService{
			Hosts: []string{sourceHost},
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
	case !equality.Semantic.DeepEqual(cur.Spec, vs.Spec):
		cur.Spec = vs.Spec
		log.Info("Entry VirtualService was updated", "name", name, "namespace", svc.Namespace)
		return r.Update(ctx, &cur) // Update the Entry VirtualService if it differs
	}
	return nil
}

func (r *FlavourRouterReconciler) ensureFlavourVS(ctx context.Context, svc *corev1.Service, directW int, wFl map[string]int) error {
	log := ctrl.LoggerFrom(ctx).WithName("[FlavourRouter]")
	name := fmt.Sprintf("%s-carbonshift-vs", svc.Name)
	host := fmt.Sprintf("%s.%s.svc.cluster.local", svc.Name, svc.Namespace)
	sourceHost := fmt.Sprintf("%s.%s.svc.cluster.local", svc.Name, svc.Namespace)

	log.Info("Ensuring Flavour VirtualService for service", "service", svc.Name)

	//highW := int32(wFl["high-power"])
	//midW := int32(wFl["mid-power"])
	//lowW := int32(wFl["low-power"])

	matches := []struct{ val, subset string }{
		{"high-power", "high-power"}, {"mid-power", "mid-power"}, {"low-power", "low-power"},
	}

	var httpRoutes []*networkingapi.HTTPRoute
	// Traffic "forced" to go to a specific flavour
	for _, m := range matches {
		httpRoutes = append(httpRoutes, &networkingapi.HTTPRoute{
			Match: []*networkingapi.HTTPMatchRequest{{
				Headers: map[string]*networkingapi.StringMatch{
					"x-carbonshift": {MatchType: &networkingapi.StringMatch_Exact{Exact: m.val}},
				},
			}},
			Route: []*networkingapi.HTTPRouteDestination{{
				Destination: &networkingapi.Destination{Host: host, Subset: m.subset},
				Weight:      100,
			}},
		})
	}

	// "Normal" traffic routing to the flavours
	//httpRoutes = append(httpRoutes, &networkingapi.HTTPRoute{
	//	Route: []*networkingapi.HTTPRouteDestination{
	//		{Destination: &networkingapi.Destination{Host: host, Subset: "high-power"}, Weight: highW},
	//		{Destination: &networkingapi.Destination{Host: host, Subset: "mid-power"}, Weight: midW},
	//		{Destination: &networkingapi.Destination{Host: host, Subset: "low-power"}, Weight: lowW},
	//	},
	//})

	vs := networkingkube.VirtualService{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: svc.Namespace},
		Spec: networkingapi.VirtualService{
			Hosts: []string{sourceHost},
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
	case !equality.Semantic.DeepEqual(cur.Spec, vs.Spec):
		cur.Spec = vs.Spec
		log.Info("Flavour VirtualService was updated", "name", name, "namespace", svc.Namespace)
		return r.Update(ctx, &cur)
	}
	return nil
}

/*
func (r *FlavourRouterReconciler) handleScaling(ctx context.Context, ns string, weights map[string]int) error {
    for _, fl := range flavours {
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
		Owns(&appsv1.Deployment{}).
		Owns(&corev1.Service{}).
		Owns(&kedav1alpha1.ScaledObject{}).
		Owns(&corev1.ServiceAccount{}).
		Owns(&rbacv1.ClusterRoleBinding{}).
		Owns(&networkingkube.DestinationRule{}).
		Owns(&networkingkube.VirtualService{}).
		Watches(&schedulingv1alpha1.TrafficSchedule{}, mapTS).
		Complete(r)
}

func (r *FlavourRouterReconciler) ensureServiceAccount(ctx context.Context, svc *corev1.Service) error {
	log := ctrl.LoggerFrom(ctx).WithName("[FlavourRouter]")
	saName := fmt.Sprintf("%s-trafficschedule-viewer", svc.Name)

	sa := &corev1.ServiceAccount{
		ObjectMeta: metav1.ObjectMeta{
			Name:      saName,
			Namespace: svc.Namespace,
		},
	}

	if err := ctrl.SetControllerReference(svc, sa, r.Scheme); err != nil {
		return err
	}

	var currentSA corev1.ServiceAccount
	err := r.Get(ctx, client.ObjectKey{Name: saName, Namespace: svc.Namespace}, &currentSA)
	if err != nil {
		if apierrors.IsNotFound(err) {
			log.Info("Creating ServiceAccount", "ServiceAccount", sa.Name)
			return r.Create(ctx, sa)
		}
		return err
	}
	return nil
}

func (r *FlavourRouterReconciler) ensureClusterRoleBinding(ctx context.Context, svc *corev1.Service) error {
	log := ctrl.LoggerFrom(ctx).WithName("[FlavourRouter]")
	saName := fmt.Sprintf("%s-trafficschedule-viewer", svc.Name)
	rbName := fmt.Sprintf("%s-trafficschedule-viewer-binding", svc.Name)

	rb := &rbacv1.ClusterRoleBinding{
		ObjectMeta: metav1.ObjectMeta{
			Name:      rbName,
			Namespace: svc.Namespace,
		},
		Subjects: []rbacv1.Subject{
			{
				Kind:      "ServiceAccount",
				Name:      saName,
				Namespace: svc.Namespace,
			},
		},
		RoleRef: rbacv1.RoleRef{
			Kind:     "ClusterRole",
			Name:     "trafficschedule-viewer-role",
			APIGroup: "rbac.authorization.k8s.io",
		},
	}

	if err := ctrl.SetControllerReference(svc, rb, r.Scheme); err != nil {
		return err
	}

	var currentRB rbacv1.ClusterRoleBinding
	err := r.Get(ctx, client.ObjectKey{Name: rbName, Namespace: svc.Namespace}, &currentRB)
	if err != nil {
		if apierrors.IsNotFound(err) {
			log.Info("Creating ClusterRoleBinding", "ClusterRoleBinding", rb.Name)
			return r.Create(ctx, rb)
		}
		return err
	}
	return nil
}

func (r *FlavourRouterReconciler) ensureBufferServiceService(ctx context.Context, svc *corev1.Service, component string) error {
	log := ctrl.LoggerFrom(ctx).WithName("[FlavourRouter]")
	serviceName := fmt.Sprintf("buffer-service-%s-%s", component, svc.Name)

	labels := map[string]string{
		"app.kubernetes.io/name":       fmt.Sprintf("buffer-service-%s", component),
		"app.kubernetes.io/instance":   "carbonshift",
		"app.kubernetes.io/component":  component,
		"app.kubernetes.io/part-of":    "carbonshift",
		"carbonshift/parent-service":   svc.Name,
		"app.kubernetes.io/managed-by": "carbonshift-operator",
	}

	var ports []corev1.ServicePort
	if component == "router" {
		ports = []corev1.ServicePort{
			{Name: "http", Port: 8000, TargetPort: intstr.FromInt(8000)},
			{Name: "metrics", Port: 8001, TargetPort: intstr.FromInt(8001)},
		}
	} else { // consumer
		ports = []corev1.ServicePort{
			{Name: "metrics", Port: 8001, TargetPort: intstr.FromInt(8001)},
		}
	}

	bufferSvc := &corev1.Service{
		ObjectMeta: metav1.ObjectMeta{
			Name:      serviceName,
			Namespace: svc.Namespace,
			Labels:    labels,
		},
		Spec: corev1.ServiceSpec{
			Selector: map[string]string{
				"app.kubernetes.io/name":     fmt.Sprintf("buffer-service-%s", component),
				"app.kubernetes.io/instance": "carbonshift",
			},
			Ports: ports,
			Type:  corev1.ServiceTypeClusterIP,
		},
	}

	if err := ctrl.SetControllerReference(svc, bufferSvc, r.Scheme); err != nil {
		return err
	}

	var currentSvc corev1.Service
	err := r.Get(ctx, client.ObjectKey{Name: serviceName, Namespace: svc.Namespace}, &currentSvc)
	if err != nil {
		if apierrors.IsNotFound(err) {
			log.Info("Creating Service", "Component", component, "Service", bufferSvc.Name)
			return r.Create(ctx, bufferSvc)
		}
		return err
	}

	// Preserve ClusterIP
	bufferSvc.Spec.ClusterIP = currentSvc.Spec.ClusterIP
	if !equality.Semantic.DeepEqual(currentSvc.Spec, bufferSvc.Spec) {
		currentSvc.Spec = bufferSvc.Spec
		log.Info("Updating Service", "Component", component, "Service", bufferSvc.Name)
		return r.Update(ctx, &currentSvc)
	}

	return nil
}

func (r *FlavourRouterReconciler) ensureBufferServiceDeployment(ctx context.Context, svc *corev1.Service, component string, resources corev1.ResourceRequirements) error {
	log := ctrl.LoggerFrom(ctx).WithName("[FlavourRouter]")
	depName := fmt.Sprintf("buffer-service-%s-%s", component, svc.Name)
	saName := fmt.Sprintf("%s-trafficschedule-viewer", svc.Name)

	labels := map[string]string{
		"app.kubernetes.io/name":       fmt.Sprintf("buffer-service-%s", component),
		"app.kubernetes.io/instance":   "carbonshift",
		"app.kubernetes.io/component":  component,
		"app.kubernetes.io/part-of":    "carbonshift",
		"carbonshift/parent-service":   svc.Name,
		"app.kubernetes.io/managed-by": "carbonshift-operator",
	}

	var annotations map[string]string
	var extraEnv []corev1.EnvVar

	if component == "consumer" {
		annotations = map[string]string{"sidecar.istio.io/inject": "true"}
		extraEnv = []corev1.EnvVar{
			{Name: "TARGET_SVC_SCHEME", Value: "http"},
			{Name: "TARGET_SVC_PORT", Value: "80"},
		}
	}

	baseEnv := []corev1.EnvVar{
		{Name: "RABBITMQ_URL", Value: "amqp://carbonuser:supersecret@carbonshift-rabbitmq.carbonshift-system.svc.cluster.local:5672"},
		{Name: "TRAFFIC_SCHEDULE_NAME", Value: "TrafficSchedule"},
		{Name: "METRICS_PORT", Value: "8001"},
		{Name: "TARGET_SVC_NAME", Value: svc.Name},
		{Name: "TARGET_SVC_NAMESPACE", ValueFrom: &corev1.EnvVarSource{FieldRef: &corev1.ObjectFieldSelector{FieldPath: "metadata.namespace"}}},
		{Name: "TS_NAME", Value: "traffic-schedule"},
		{Name: "DEBUG", Value: "true"},
		{Name: "PYTHONUNBUFFERED", Value: "1"},
	}

	allEnv := append(baseEnv, extraEnv...)

	dep := &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{
			Name:      depName,
			Namespace: svc.Namespace,
			Labels:    labels,
		},
		Spec: appsv1.DeploymentSpec{
			Replicas: ptr.To[int32](1),
			Selector: &metav1.LabelSelector{
				MatchLabels: map[string]string{
					"app.kubernetes.io/name":     fmt.Sprintf("buffer-service-%s", component),
					"app.kubernetes.io/instance": "carbonshift",
				},
			},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{
					Labels:      labels,
					Annotations: annotations,
				},
				Spec: corev1.PodSpec{
					ServiceAccountName: saName,
					Containers: []corev1.Container{
						{
							Name:            fmt.Sprintf("buffer-service-%s", component),
							Image:           fmt.Sprintf("ghcr.io/belgio99/k8s-carbonshift/buffer-service-%s:latest", component),
							ImagePullPolicy: corev1.PullAlways,
							Env:             allEnv,
							Resources:       resources,
						},
					},
				},
			},
		},
	}

	if err := ctrl.SetControllerReference(svc, dep, r.Scheme); err != nil {
		return err
	}

	var currentDep appsv1.Deployment
	err := r.Get(ctx, client.ObjectKey{Name: depName, Namespace: svc.Namespace}, &currentDep)
	if err != nil {
		if apierrors.IsNotFound(err) {
			log.Info("Creating Deployment", "Component", component, "Deployment", dep.Name)
			return r.Create(ctx, dep)
		}
		return err
	}

	desired := dep.Spec
   desired.Replicas = nil

   current := currentDep.Spec
   current.Replicas = nil

   if !equality.Semantic.DeepEqual(current, desired) {
      log.Info("Updating Deployment", "Component", component, "Deployment", dep.Name)

      return retry.RetryOnConflict(retry.DefaultRetry, func() error {
         var latest appsv1.Deployment
         if err := r.Get(ctx, client.ObjectKey{Name: depName, Namespace: svc.Namespace}, &latest); err != nil {
            return err
         }
         // manteniamo le Replicas attuali (gestite dall’HPA/KEDA)
         dep.Spec.Replicas = latest.Spec.Replicas

         latest.Spec = dep.Spec
         return r.Update(ctx, &latest)
      })
   }

	return nil
}

func (r *FlavourRouterReconciler) ensureConsumerScaledObject(ctx context.Context, svc *corev1.Service, autoscaling schedulingv1alpha1.AutoscalingConfig) error {
	log := ctrl.LoggerFrom(ctx).WithName("[FlavourRouter]")
	soName := fmt.Sprintf("buffer-service-consumer-%s", svc.Name)
	targetName := fmt.Sprintf("buffer-service-consumer-%s", svc.Name)

	so := &kedav1alpha1.ScaledObject{
		ObjectMeta: metav1.ObjectMeta{
			Name:      soName,
			Namespace: svc.Namespace,
		},
		Spec: kedav1alpha1.ScaledObjectSpec{
			ScaleTargetRef:  &kedav1alpha1.ScaleTarget{Name: targetName},
			PollingInterval: ptr.To[int32](5),
			CooldownPeriod:  autoscaling.CooldownPeriod,
			MinReplicaCount: autoscaling.MinReplicaCount,
			MaxReplicaCount: autoscaling.MaxReplicaCount,
			Triggers: []kedav1alpha1.ScaleTriggers{
				{
					Type:              "rabbitmq",
					AuthenticationRef: &kedav1alpha1.AuthenticationRef{Name: "carbonshift-rabbitmq-auth", Kind: "ClusterTriggerAuthentication"},
					Metadata:          map[string]string{"queueName": fmt.Sprintf("%s.%s.direct.low-power", svc.Namespace, svc.Name), "mode": "QueueLength", "value": "1000000"},
				},
				{
					Type:              "rabbitmq",
					AuthenticationRef: &kedav1alpha1.AuthenticationRef{Name: "carbonshift-rabbitmq-auth", Kind: "ClusterTriggerAuthentication"},
					Metadata:          map[string]string{"queueName": fmt.Sprintf("%s.%s.direct.mid-power", svc.Namespace, svc.Name), "mode": "QueueLength", "value": "1000000"},
				},
				{
					Type:              "rabbitmq",
					AuthenticationRef: &kedav1alpha1.AuthenticationRef{Name: "carbonshift-rabbitmq-auth", Kind: "ClusterTriggerAuthentication"},
					Metadata:          map[string]string{"queueName": fmt.Sprintf("%s.%s.direct.high-power", svc.Namespace, svc.Name), "mode": "QueueLength", "value": "1000000"},
				},
				{
					Type: "cpu",
					Metadata: map[string]string{
						"type":  "Utilization",
						"value": fmt.Sprintf("%d", *autoscaling.CPUUtilization),
					},
				},
				{
					Type: "prometheus",
					Metadata: map[string]string{
						"serverAddress":       "http://carbonshift-kube-prometheu-prometheus.carbonshift-system.svc:9090",
						"query":               "sum(increase(consumer_http_requests_created[60s]))",
						"threshold":           "1000000",
						"activationThreshold": "1",
					},
				},
				{
					Type: "prometheus",
					Metadata: map[string]string{
						"serverAddress": "http://carbonshift-kube-prometheu-prometheus.carbonshift-system.svc:9090",
						"query":         fmt.Sprintf(`sum(rabbitmq_queue_messages_ready{queue=~"^%s\\.%s\\.queue\\..+"}) * max(schedule_consumption_enabled)`, svc.Namespace, svc.Name),
						"threshold":     "1",
					},
				},
			},
		},
	}

	if err := ctrl.SetControllerReference(svc, so, r.Scheme); err != nil {
		return err
	}

	var currentSO kedav1alpha1.ScaledObject
	err := r.Get(ctx, client.ObjectKey{Name: soName, Namespace: svc.Namespace}, &currentSO)
	if err != nil {
		if apierrors.IsNotFound(err) {
			log.Info("Creating Consumer ScaledObject", "ScaledObject", so.Name)
			return r.Create(ctx, so)
		}
		return err
	}

	if !equality.Semantic.DeepEqual(currentSO.Spec, so.Spec) {
		currentSO.Spec = so.Spec
		log.Info("Updating Consumer ScaledObject", "ScaledObject", so.Name)
		return r.Update(ctx, &currentSO)
	}

	return nil
}

func (r *FlavourRouterReconciler) ensureFlavourScaledObject(ctx context.Context, svc *corev1.Service, flavour string, autoscaling schedulingv1alpha1.AutoscalingConfig) error {
	log := ctrl.LoggerFrom(ctx).WithName("[FlavourRouter]")
	// flavour is "high-power", "mid-power", or "low-power"
	// The target deployment is named like "carbonstat-low-power"
	soName := fmt.Sprintf("%s-%s", svc.Name, flavour)
	targetName := fmt.Sprintf("%s-%s", svc.Name, flavour)
	queueFlavour := flavour // e.g. low-power

	so := &kedav1alpha1.ScaledObject{
		ObjectMeta: metav1.ObjectMeta{
			Name:      soName,
			Namespace: svc.Namespace,
		},
		Spec: kedav1alpha1.ScaledObjectSpec{
			ScaleTargetRef:  &kedav1alpha1.ScaleTarget{Name: targetName},
			PollingInterval: ptr.To[int32](5),
			CooldownPeriod:  autoscaling.CooldownPeriod,
			MinReplicaCount: autoscaling.MinReplicaCount,
			MaxReplicaCount: autoscaling.MaxReplicaCount,
			Triggers: []kedav1alpha1.ScaleTriggers{
				{
					Type: "prometheus",
					Metadata: map[string]string{
						"serverAddress":       "http://carbonshift-kube-prometheu-prometheus.carbonshift-system.svc:9090",
						"query":               fmt.Sprintf(`sum(max_over_time(rabbitmq_queue_messages_ready{queue="%s.%s.queue.%s"}[30s])) * max(schedule_consumption_enabled)`, svc.Namespace, svc.Name, queueFlavour),
						"threshold":           "1000000",
						"activationThreshold": "1",
					},
				},
				{
					Type:              "rabbitmq",
					AuthenticationRef: &kedav1alpha1.AuthenticationRef{Name: "carbonshift-rabbitmq-auth", Kind: "ClusterTriggerAuthentication"},
					Metadata:          map[string]string{"queueName": fmt.Sprintf("%s.%s.direct.%s", svc.Namespace, svc.Name, queueFlavour), "mode": "QueueLength", "value": "1000000"},
				},
				{
					Type: "cpu",
					Metadata: map[string]string{
						"type":  "Utilization",
						"value": fmt.Sprintf("%d", *autoscaling.CPUUtilization),
					},
				},
			},
		},
	}

	if err := ctrl.SetControllerReference(svc, so, r.Scheme); err != nil {
		return err
	}

	var currentSO kedav1alpha1.ScaledObject
	err := r.Get(ctx, client.ObjectKey{Name: soName, Namespace: svc.Namespace}, &currentSO)
	if err != nil {
		if apierrors.IsNotFound(err) {
			log.Info("Creating Flavour ScaledObject", "ScaledObject", so.Name)
			return r.Create(ctx, so)
		}
		return err
	}

	if !equality.Semantic.DeepEqual(currentSO.Spec, so.Spec) {
		currentSO.Spec = so.Spec
		log.Info("Updating Flavour ScaledObject", "ScaledObject", so.Name)
		return r.Update(ctx, &currentSO)
	}

	return nil
}
