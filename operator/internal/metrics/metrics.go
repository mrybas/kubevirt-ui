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

// Package metrics holds operator-wide Prometheus metrics.
package metrics

import (
	"github.com/prometheus/client_golang/prometheus"
	"sigs.k8s.io/controller-runtime/pkg/metrics"
)

// PatchesTotal counts writes the operator actually sent to the API server.
//
// It exists because "write only when something changed" is an invariant we
// cannot observe in production from logs alone: a controller that patches an
// unchanged object looks identical to one that does not, right up until the
// downstream system (frr-k8s reload, CDI re-import) reacts to the churn. The
// counter makes the invariant measurable outside tests — a flat line is the
// proof, a climbing one on an idle cluster is the bug report.
//
// op is one of "created", "updated", "deleted", "status".
var PatchesTotal = prometheus.NewCounterVec(
	prometheus.CounterOpts{
		Name: "kubevirt_ui_operator_patches_total",
		Help: "Writes issued by the operator, by object kind, controller and operation.",
	},
	[]string{"kind", "controller", "op"},
)

// ReconcileErrorsTotal counts reconcile passes that ended in an error, by
// controller and a short reason. Reason is a coarse bucket, never a message —
// messages belong in conditions and events, cardinality belongs to nobody.
var ReconcileErrorsTotal = prometheus.NewCounterVec(
	prometheus.CounterOpts{
		Name: "kubevirt_ui_operator_reconcile_errors_total",
		Help: "Reconcile passes that returned an error, by controller and reason bucket.",
	},
	[]string{"controller", "reason"},
)

// GuardDecisionsTotal counts what the raw-VirtualMachine guard decided.
//
// A guard that is never consulted is indistinguishable from one that finds
// nothing to refuse, so the allowed count matters as much as the denied one.
var GuardDecisionsTotal = prometheus.NewCounterVec(
	prometheus.CounterOpts{
		Name: "kubevirt_ui_operator_guard_decisions_total",
		Help: "Admission decisions made by the raw VirtualMachine guard.",
	},
	[]string{"decision"},
)

// GuardWired is 1 when the guard's webhook configuration is actually usable —
// it exists, it points at this operator, and it carries a CA bundle.
//
// This gauge exists because the guard fails open on purpose. A fail-open
// webhook that is not wired up behaves exactly like one that is, from every
// side except the thing it was meant to stop: measured once already, an empty
// caBundle let a cluster-admin create a raw VirtualMachine with no error
// anywhere except "tls: bad certificate" in this operator's own log. The policy
// is only safe if the misconfiguration is loud.
var GuardWired = prometheus.NewGauge(
	prometheus.GaugeOpts{
		Name: "kubevirt_ui_operator_guard_wired",
		Help: "1 when the raw VirtualMachine guard's webhook configuration is usable, 0 otherwise.",
	},
)

// GuardLastCheck is when the wiring was last examined, as a unix timestamp.
//
// It exists because a gauge that has never been set reads exactly the same as
// one set to zero, and "the guard is broken" and "nobody has looked yet" are
// very different things to be woken up for. Caught while testing the gauge
// above: the first negative result was read eight seconds after a restart,
// before the watchdog had run at all, and looked like proof.
var GuardLastCheck = prometheus.NewGauge(
	prometheus.GaugeOpts{
		Name: "kubevirt_ui_operator_guard_last_check_timestamp_seconds",
		Help: "Unix time of the last raw VirtualMachine guard wiring check; absent means never checked.",
	},
)

func init() {
	metrics.Registry.MustRegister(
		PatchesTotal, ReconcileErrorsTotal, GuardDecisionsTotal, GuardWired, GuardLastCheck,
	)
}
