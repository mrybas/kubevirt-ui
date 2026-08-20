/*
Copyright 2026.

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
	"context"
	"strings"
	"testing"

	admissionregistrationv1 "k8s.io/api/admissionregistration/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	utilruntime "k8s.io/apimachinery/pkg/util/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
)

func watchdogFor(objs ...*admissionregistrationv1.ValidatingWebhookConfiguration) *GuardWatchdog {
	scheme := runtime.NewScheme()
	utilruntime.Must(clientgoscheme.AddToScheme(scheme))
	builder := fake.NewClientBuilder().WithScheme(scheme)
	for _, o := range objs {
		builder = builder.WithObjects(o)
	}
	return &GuardWatchdog{
		Client:           builder.Build(),
		ConfigName:       func() string { return "guard-config" },
		ServiceNamespace: "kubevirt-ui-operator-dev",
	}
}

func config(caBundle []byte, serviceNS string) *admissionregistrationv1.ValidatingWebhookConfiguration {
	return &admissionregistrationv1.ValidatingWebhookConfiguration{
		ObjectMeta: metav1.ObjectMeta{Name: "guard-config"},
		Webhooks: []admissionregistrationv1.ValidatingWebhook{{
			Name: guardWebhookName,
			ClientConfig: admissionregistrationv1.WebhookClientConfig{
				CABundle: caBundle,
				Service: &admissionregistrationv1.ServiceReference{
					Name:      "webhook-service",
					Namespace: serviceNS,
				},
			},
		}},
	}
}

// The exact failure that shipped once: cert-manager injected nothing because
// the annotation named a namespace the certificate was not in, so the API
// server could not verify the webhook and — the policy being fail-open —
// admitted every raw VirtualMachine with no error visible anywhere.
func TestAnEmptyCABundleIsReportedAsNotGuarding(t *testing.T) {
	err := watchdogFor(config(nil, "kubevirt-ui-operator-dev")).wired(context.Background())
	if err == nil {
		t.Fatal("a webhook with no CA bundle was reported as wired")
	}
	if !strings.Contains(err.Error(), "CA bundle") {
		t.Fatalf("the reason does not name the problem: %v", err)
	}
}

func TestAMissingConfigurationIsReported(t *testing.T) {
	if err := watchdogFor().wired(context.Background()); err == nil {
		t.Fatal("a missing webhook configuration was reported as wired")
	}
}

func TestAConfigurationPointingSomewhereElseIsReported(t *testing.T) {
	err := watchdogFor(config([]byte("ca"), "someone-elses-namespace")).wired(context.Background())
	if err == nil {
		t.Fatal("a webhook routing to another operator was reported as wired")
	}
}

func TestAWiredGuardReportsClean(t *testing.T) {
	if err := watchdogFor(config([]byte("ca"), "kubevirt-ui-operator-dev")).wired(context.Background()); err != nil {
		t.Fatalf("a correctly wired guard was reported as broken: %v", err)
	}
}

func TestAnUnconfiguredWatchdogSaysSoRatherThanClaimingHealth(t *testing.T) {
	w := watchdogFor(config([]byte("ca"), "kubevirt-ui-operator-dev"))
	w.ConfigName = func() string { return "" }
	if err := w.wired(context.Background()); err == nil {
		t.Fatal("a watchdog that cannot find its configuration claimed the guard was fine")
	}
}
