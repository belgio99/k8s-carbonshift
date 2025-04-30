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

	istionetworkingv1 "istio.io/client-go/pkg/apis/networking/v1alpha3"
	"k8s.io/apimachinery/pkg/runtime"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	schedulingv1alpha1 "github.com/belgio/k8s-carbonaware-scheduler/operator/api/v1alpha1"
)

// TrafficScheduleReconciler reconciles a TrafficSchedule object
type TrafficScheduleReconciler struct {
	client.Client
	Scheme *runtime.Scheme
}

// +kubebuilder:rbac:groups=scheduling.carbonshift.io,resources=trafficschedules,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=scheduling.carbonshift.io,resources=trafficschedules/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=scheduling.carbonshift.io,resources=trafficschedules/finalizers,verbs=update
//+kubebuilder:rbac:groups=networking.istio.io,resources=virtualservices,verbs=get;list;watch;create;update;patch;delete
//+kubebuilder:rbac:groups=networking.istio.io,resources=destinationrules,verbs=get;list;watch;create;update;patch;delete

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
	logs := logf.FromContext(ctx)

	// Define and fetch the TrafficSchedule instance
	var ts schedulingv1alpha1.TrafficSchedule
	if err := r.Get(ctx, req.NamespacedName, &ts); err != nil {
		logs.Error(err, "unable to fetch TrafficSchedule")
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}

	logs.Info("Reconciling TrafficSchedule", "name", ts.Name)

	//Update Istio VirtualService weights with the new traffic schedule
	lowWeight := int32(ts.Spec.FlavorRules[0].Percentage)
	highWeight := int32(ts.Spec.FlavorRules[1].Percentage)



	return ctrl.Result{}, nil
}

// SetupWithManager sets up the controller with the Manager.
func (r *TrafficScheduleReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&schedulingv1alpha1.TrafficSchedule{}).
		Named("trafficschedule").
		Complete(r)
}
