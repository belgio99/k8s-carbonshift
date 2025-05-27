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
package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// EDIT THIS FILE!  THIS IS SCAFFOLDING FOR YOU TO OWN!
// NOTE: json tags are required.  Any new fields you add must have json tags for the fields to be serialized.

// TrafficScheduleSpec defines the desired state of TrafficSchedule.
type TrafficScheduleSpec struct {
	// INSERT ADDITIONAL SPEC FIELDS - desired state of cluster
	// Important: Run "make" to regenerate code after modifying this file

	FlavorRules []FlavorRule `json:"flavorRules"`
	// DirectWeight: how much of the traffic should be scheduled directly to the application (percentage)
	DirectWeight int `json:"directWeight"`
	// QueueWeight: how much of the traffic should be scheduled to the queue (percentage)
	QueueWeight int `json:"queueWeight"`
	// ValidUntil: when the schedule is valid
	ValidUntil metav1.Time `json:"validUntil"`
}

// FlavorRule defines the rules for a specific flavor.
type FlavorRule struct {
	FlavorName string `json:"flavorName"`
	// Weight: how much of the traffic should be scheduled to this flavor (percentage)
	Weight int `json:"weight"`
	// DeadlineSec: max delay in seconds
	DeadlineSec int `json:"deadlineSec"`
}

// TrafficScheduleStatus defines the observed state of TrafficSchedule.
type TrafficScheduleStatus struct {
	// INSERT ADDITIONAL STATUS FIELD - define observed state of cluster
	// Important: Run "make" to regenerate code after modifying this file
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:resource:scope=Cluster

// TrafficSchedule is the Schema for the trafficschedules API.
type TrafficSchedule struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   TrafficScheduleSpec   `json:"spec,omitempty"`
	Status TrafficScheduleStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// TrafficScheduleList contains a list of TrafficSchedule.
type TrafficScheduleList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []TrafficSchedule `json:"items"`
}

func init() {
	SchemeBuilder.Register(&TrafficSchedule{}, &TrafficScheduleList{})
}
