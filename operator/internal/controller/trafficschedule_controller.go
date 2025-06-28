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
	"sort"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/builder"
	"sigs.k8s.io/controller-runtime/pkg/event"
	"sigs.k8s.io/controller-runtime/pkg/predicate"

	schedulingv1alpha1 "github.com/belgio99/k8s-carbonshift/operator/api/v1alpha1"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// TrafficScheduleReconciler reconciles a TrafficSchedule object
type TrafficScheduleReconciler struct {
	client.Client
	Scheme *runtime.Scheme
}

const pollInterval = 1 * time.Minute

// +kubebuilder:rbac:groups=scheduling.carbonshift.io,resources=trafficschedules,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=scheduling.carbonshift.io,resources=trafficschedules/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=scheduling.carbonshift.io,resources=trafficschedules/finalizers,verbs=update


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
		FlavourWeights map[string]int `json:"flavourWeights"`
		Deadlines     map[string]int `json:"deadlines"`
		ConsumptionEnabled bool `json:"consumptionEnabled"`
		ValidUntilISO string         `json:"validUntil"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&remote); err != nil {
		ctrl.Log.Error(err, "Failed to decode traffic schedule response")
		return ctrl.Result{}, err
	}

	// 3) Create the status for the TrafficSchedule CR
	status := schedulingv1alpha1.TrafficScheduleStatus{
		DirectWeight: int(remote.DirectWeight),
		QueueWeight:  int(remote.QueueWeight),
		ConsumptionEnabled: remote.ConsumptionEnabled,
	}
	for flavour, w := range remote.FlavourWeights {
		dl := remote.Deadlines[flavour]
		status.FlavourRules = append(status.FlavourRules, schedulingv1alpha1.FlavourRule{
			FlavourName:  flavour,
			Weight:      int(w),
			DeadlineSec: int(dl),
		})
	}
	if t, err := time.Parse(time.RFC3339, remote.ValidUntilISO); err == nil {
		status.ValidUntil = metav1.NewTime(t)
	}

	sort.Slice(status.FlavourRules, func(i, j int) bool {
    return status.FlavourRules[i].FlavourName < status.FlavourRules[j].FlavourName
	})

	// 4) Overwrite old status with the new one
   statusChanged := !reflect.DeepEqual(existing.Status, status)
	if statusChanged {
   	existing.Status = status
	if err := r.Status().Update(ctx, &existing); err != nil {
      ctrl.Log.Error(err, "unable to update TrafficSchedule status")
		return ctrl.Result{}, err
      }
   }
	next := pollInterval
   if !status.ValidUntil.IsZero() {
       until := time.Until(status.ValidUntil.Time)
       if until < 0 {
           until = 0
       }
       if until < next { 
           next = until
       }
   }

   ctrl.Log.Info("TrafficSchedule reconcile complete",
       "nextReconcileIn", next)

   return ctrl.Result{RequeueAfter: next}, nil
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
