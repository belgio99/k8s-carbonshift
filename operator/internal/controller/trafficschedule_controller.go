/*
Copyright 2025 belgio99.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package controller

import (
	"context"
	"encoding/json"
	"net/http"
	"reflect"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/builder"
	"sigs.k8s.io/controller-runtime/pkg/event"
	"sigs.k8s.io/controller-runtime/pkg/predicate"

	schedulingv1alpha1 "github.com/belgio/k8s-carbonaware-scheduler/operator/api/v1alpha1"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// TrafficScheduleReconciler reconciles a TrafficSchedule object
type TrafficScheduleReconciler struct {
	client.Client
	Scheme *runtime.Scheme
}

// +kubebuilder:rbac:groups=scheduling.carbonshift.io,resources=trafficschedules,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=scheduling.carbonshift.io,resources=trafficschedules/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=scheduling.carbonshift.io,resources=trafficschedules/finalizers,verbs=update
// +kubebuilder:rbac:groups=networking.istio.io,resources=virtualservices,verbs=get;list;watch;create;update;patch;delete

// Reconcile is part of the main kubernetes reconciliation loop which aims to
// move the current state of the cluster closer to the desired state.
// TODO(user): Modify the Reconcile function to compare the state specified by
// the TrafficSchedule object against the actual cluster state, and then
// perform operations to make the cluster state reflect the state specified by
// the user.
//
// For more details, check Reconcile and its Result here:
// - https://pkg.go.dev/sigs.k8s.io/controller-runtime@v0.20.4/pkg/reconcile
func (r *TrafficScheduleReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {

	ctrl.Log.Info("Reconciling TrafficSchedule", "name", req.Name)

	var existing schedulingv1alpha1.TrafficSchedule
	if err := r.Get(ctx, req.NamespacedName, &existing); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}

	// 1) Get schedule from decision engine
	resp, err := http.Get("http://carbonshift-decision-engine.carbonshift-system.svc.cluster.local/schedule")
	if err != nil {
		ctrl.Log.Error(err, "Failed to get traffic schedule")
		return ctrl.Result{}, err
	}
	defer resp.Body.Close()

	// 2) Temp struct to decode the response
	var remote struct {
		DirectWeight  int            `json:"directWeight"`
		QueueWeight   int            `json:"queueWeight"`
		FlavorWeights map[string]int `json:"flavorWeights"`
		Deadlines     map[string]int `json:"deadlines"`
		ValidUntilISO string         `json:"validUntil"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&remote); err != nil {
		ctrl.Log.Error(err, "Failed to decode traffic schedule response")
		return ctrl.Result{}, err
	}

	// 3) Create the spec for the TrafficSchedule CR
	spec := schedulingv1alpha1.TrafficScheduleSpec{
		DirectWeight: int(remote.DirectWeight),
		QueueWeight:  int(remote.QueueWeight),
	}
	for flavor, w := range remote.FlavorWeights {
		dl := remote.Deadlines[flavor]
		spec.FlavorRules = append(spec.FlavorRules, schedulingv1alpha1.FlavorRule{
			FlavorName:  flavor,
			Weight:      int(w),
			DeadlineSec: int(dl),
		})
	}
	if t, err := time.Parse(time.RFC3339, remote.ValidUntilISO); err == nil {
		spec.ValidUntil = metav1.NewTime(t)
	}

	// 4) Overwrite old spec with the new one
	existing.Spec = spec
	if err := r.Update(ctx, &existing); err != nil {
		ctrl.Log.Error(err, "Failed to update TrafficSchedule")
		return ctrl.Result{}, err
	}

	// 5) Requeue if the schedule is valid until a specific time
	if !existing.Spec.ValidUntil.Time.IsZero() {
		ctrl.Log.Info("TrafficSchedule reconcile complete. Next reconcile time: "+existing.Spec.ValidUntil.Time.String(), "name", req.Name)
		return ctrl.Result{RequeueAfter: time.Until(existing.Spec.ValidUntil.Time)}, nil
	}
	return ctrl.Result{}, nil
}

// SetupWithManager sets up the controller with the Manager.
func (r *TrafficScheduleReconciler) SetupWithManager(mgr ctrl.Manager) error {
	// filter: only reconcile on create or spec update
	p := predicate.Funcs{
		CreateFunc: func(e event.CreateEvent) bool { return true },
		UpdateFunc: func(e event.UpdateEvent) bool {
			oldTS := e.ObjectOld.(*schedulingv1alpha1.TrafficSchedule)
			newTS := e.ObjectNew.(*schedulingv1alpha1.TrafficSchedule)
			return !reflect.DeepEqual(oldTS.Spec, newTS.Spec)
		},
	}

	return ctrl.NewControllerManagedBy(mgr).
		For(&schedulingv1alpha1.TrafficSchedule{}, builder.WithPredicates(p)).
		Complete(r)
}
