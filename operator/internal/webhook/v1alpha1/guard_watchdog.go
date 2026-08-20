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
	"fmt"
	"os"
	"time"

	admissionregistrationv1 "k8s.io/api/admissionregistration/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/manager"

	"github.com/mrybas/kubevirt-ui/operator/internal/metrics"
)

// GuardWebhookNameEnv names the ValidatingWebhookConfiguration to watch.
const GuardWebhookNameEnv = "OPERATOR_GUARD_WEBHOOK_CONFIG"

// guardWebhookName is the entry inside that configuration.
const guardWebhookName = "guard-virtualmachine.kb.io"

// guardCheckInterval is how often the wiring is re-checked. Slow on purpose:
// this catches a deployment mistake, not a live incident, and the answer only
// changes when someone applies something.
const guardCheckInterval = 2 * time.Minute

// +kubebuilder:rbac:groups=admissionregistration.k8s.io,resources=validatingwebhookconfigurations,verbs=get;list;watch

// GuardWatchdog reports whether the raw-VM guard is actually in the request
// path.
//
// The guard fails open by design, so a broken wiring is silent by construction:
// requests that should be refused are admitted and nothing anywhere says so.
// This turns that into a number and a log line. It checks the three things that
// were actually wrong once — the configuration exists, it routes to this
// operator's service, and cert-manager has injected a CA bundle — rather than
// trying to infer health from traffic, which cannot distinguish "not consulted"
// from "nothing to refuse".
type GuardWatchdog struct {
	client.Client
	// ConfigName returns the name of the ValidatingWebhookConfiguration.
	ConfigName func() string
	// ServiceNamespace is where this operator's webhook service lives.
	ServiceNamespace string
	// Interval between checks; zero uses the default.
	Interval time.Duration
}

// SetupGuardWatchdogWithManager adds the watchdog to the manager.
func SetupGuardWatchdogWithManager(mgr ctrl.Manager, serviceNamespace string) error {
	return mgr.Add(&GuardWatchdog{
		Client:           mgr.GetClient(),
		ConfigName:       func() string { return os.Getenv(GuardWebhookNameEnv) },
		ServiceNamespace: serviceNamespace,
	})
}

// NeedLeaderElection keeps one reporter per cluster rather than one per replica.
func (w *GuardWatchdog) NeedLeaderElection() bool { return true }

// Start runs the check until the manager stops.
func (w *GuardWatchdog) Start(ctx context.Context) error {
	interval := w.Interval
	if interval == 0 {
		interval = guardCheckInterval
	}
	ticker := time.NewTicker(interval)
	defer ticker.Stop()

	w.check(ctx)
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-ticker.C:
			w.check(ctx)
		}
	}
}

func (w *GuardWatchdog) check(ctx context.Context) {
	log := logf.FromContext(ctx).WithName("guard-watchdog")

	if err := w.wired(ctx); err != nil {
		metrics.GuardWired.Set(0)
		// Warning, not debug: this says the fence around raw VirtualMachine
		// creation is not standing, and because the guard fails open nothing
		// else will mention it.
		log.Info("the raw VirtualMachine guard is NOT in the request path; "+
			"raw VirtualMachines are being admitted unchecked", "reason", err.Error())
		return
	}
	metrics.GuardWired.Set(1)
	log.V(1).Info("raw VirtualMachine guard is wired")
}

func (w *GuardWatchdog) wired(ctx context.Context) error {
	name := ""
	if w.ConfigName != nil {
		name = w.ConfigName()
	}
	if name == "" {
		return fmt.Errorf("%s is not set, so the guard's configuration cannot be checked",
			GuardWebhookNameEnv)
	}

	cfg := &admissionregistrationv1.ValidatingWebhookConfiguration{}
	if err := w.Get(ctx, types.NamespacedName{Name: name}, cfg); err != nil {
		if apierrors.IsNotFound(err) {
			return fmt.Errorf("ValidatingWebhookConfiguration %q does not exist", name)
		}
		return fmt.Errorf("reading ValidatingWebhookConfiguration %q: %w", name, err)
	}

	for _, hook := range cfg.Webhooks {
		if hook.Name != guardWebhookName {
			continue
		}
		if len(hook.ClientConfig.CABundle) == 0 {
			return fmt.Errorf(
				"webhook %q has no CA bundle, so the API server cannot verify it and "+
					"— with failurePolicy Ignore — admits everything", guardWebhookName)
		}
		svc := hook.ClientConfig.Service
		if svc == nil {
			return fmt.Errorf("webhook %q does not route to a service", guardWebhookName)
		}
		if w.ServiceNamespace != "" && svc.Namespace != w.ServiceNamespace {
			return fmt.Errorf(
				"webhook %q routes to a service in namespace %q, but this operator serves from %q",
				guardWebhookName, svc.Namespace, w.ServiceNamespace)
		}
		return nil
	}
	return fmt.Errorf("ValidatingWebhookConfiguration %q has no webhook named %q",
		name, guardWebhookName)
}

var _ manager.Runnable = &GuardWatchdog{}
