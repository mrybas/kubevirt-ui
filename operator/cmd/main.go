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

package main

import (
	"crypto/tls"
	"flag"
	"os"
	"strings"

	// Import all Kubernetes client auth plugins (e.g. Azure, GCP, OIDC, etc.)
	// to ensure that exec-entrypoint and run can make use of them.
	_ "k8s.io/client-go/plugin/pkg/client/auth"

	"k8s.io/apimachinery/pkg/runtime"
	utilruntime "k8s.io/apimachinery/pkg/util/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/healthz"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
	"sigs.k8s.io/controller-runtime/pkg/metrics/filters"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"
	"sigs.k8s.io/controller-runtime/pkg/webhook"

	clonev1beta1 "kubevirt.io/api/clone/v1beta1"
	kubevirtv1 "kubevirt.io/api/core/v1"
	snapshotv1beta1 "kubevirt.io/api/snapshot/v1beta1"
	cdiv1 "kubevirt.io/containerized-data-importer-api/pkg/apis/core/v1beta1"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
	"github.com/mrybas/kubevirt-ui/operator/internal/controller"
	"github.com/mrybas/kubevirt-ui/operator/internal/domains"
	webhookv1alpha1 "github.com/mrybas/kubevirt-ui/operator/internal/webhook/v1alpha1"
	// +kubebuilder:scaffold:imports
)

var (
	scheme   = runtime.NewScheme()
	setupLog = ctrl.Log.WithName("setup")
)

func init() {
	utilruntime.Must(clientgoscheme.AddToScheme(scheme))

	utilruntime.Must(platformv1alpha1.AddToScheme(scheme))
	// CDI objects are written by the image controller and read by everything
	// downstream of it.
	utilruntime.Must(cdiv1.AddToScheme(scheme))
	utilruntime.Must(kubevirtv1.AddToScheme(scheme))
	utilruntime.Must(snapshotv1beta1.AddToScheme(scheme))
	utilruntime.Must(clonev1beta1.AddToScheme(scheme))
	// +kubebuilder:scaffold:scheme
}

// nolint:gocyclo
// splitList turns a comma-separated flag into a list, dropping the empties a
// trailing comma leaves behind.
func splitList(value string) []string {
	var out []string
	for _, item := range strings.Split(value, ",") {
		if item = strings.TrimSpace(item); item != "" {
			out = append(out, item)
		}
	}
	return out
}

func main() {
	var metricsAddr string
	var metricsCertPath, metricsCertName, metricsCertKey string
	var webhookCertPath, webhookCertName, webhookCertKey string
	var enableLeaderElection bool
	var probeAddr string
	var secureMetrics bool
	var enableHTTP2 bool
	var domainsFlag string
	var serviceCIDRFlag string
	var tenantSupernetFlag string
	var mgmtCIDRFlag string
	var tlsOpts []func(*tls.Config)
	flag.StringVar(&metricsAddr, "metrics-bind-address", "0", "The address the metrics endpoint binds to. "+
		"Use :8443 for HTTPS or :8080 for HTTP, or leave as 0 to disable the metrics service.")
	flag.StringVar(&probeAddr, "health-probe-bind-address", ":8081", "The address the probe endpoint binds to.")
	flag.BoolVar(&enableLeaderElection, "leader-elect", false,
		"Enable leader election for controller manager. "+
			"Enabling this will ensure there is only one active controller manager.")
	flag.BoolVar(&secureMetrics, "metrics-secure", true,
		"If set, the metrics endpoint is served securely via HTTPS. Use --metrics-secure=false to use HTTP instead.")
	flag.StringVar(&webhookCertPath, "webhook-cert-path", "", "The directory that contains the webhook certificate.")
	flag.StringVar(&webhookCertName, "webhook-cert-name", "tls.crt", "The name of the webhook certificate file.")
	flag.StringVar(&webhookCertKey, "webhook-cert-key", "tls.key", "The name of the webhook key file.")
	flag.StringVar(&metricsCertPath, "metrics-cert-path", "",
		"The directory that contains the metrics server certificate.")
	flag.StringVar(&metricsCertName, "metrics-cert-name", "tls.crt", "The name of the metrics server certificate file.")
	flag.StringVar(&metricsCertKey, "metrics-cert-key", "tls.key", "The name of the metrics server key file.")
	flag.BoolVar(&enableHTTP2, "enable-http2", false,
		"If set, HTTP/2 will be enabled for the metrics and webhook servers")
	flag.StringVar(&tenantSupernetFlag, "tenant-supernet", "",
		"The aggregate every tenant network is carved from, e.g. 10.200.0.0/14. "+
			"The isolation floor is scoped to it, so one rule denies every other "+
			"tenant instead of one rule per tenant. Empty means no isolation is "+
			"written at all: a drop with nothing to scope it to would take the "+
			"internet with it.")
	flag.StringVar(&mgmtCIDRFlag, "mgmt-cidr", "",
		"Comma-separated prefixes of the management plane, which must not open "+
			"connections into a tenant. Empty means each node's own address as a "+
			"/32 — exact, and it cannot over-block. Autodiscovery cannot learn the "+
			"mask: the API reports node addresses, not the network they sit on.")
	flag.StringVar(&serviceCIDRFlag, "service-cidr", "",
		"The cluster's service network, e.g. 10.96.0.0/12. Empty means read it "+
			"from the cluster: the kubeadm ConfigMap, else the apiserver's own "+
			"--service-cluster-ip-range. Set this where neither is readable — a "+
			"managed control plane exposes no apiserver pod, and VPC DNS needs "+
			"the range to route to the cluster resolver.")
	flag.StringVar(&domainsFlag, "domains", domains.VM,
		"Comma-separated controller domains this process runs: "+
			"vm, network, tenant, remediation. Each domain is deployed separately so "+
			"one crashing domain cannot stop the others.")
	opts := zap.Options{
		Development: true,
	}
	opts.BindFlags(flag.CommandLine)
	flag.Parse()

	ctrl.SetLogger(zap.New(zap.UseFlagOptions(&opts)))

	enabled, err := domains.Parse(domainsFlag)
	if err != nil {
		setupLog.Error(err, "Invalid --domains")
		os.Exit(1)
	}
	setupLog.Info("Domains enabled", "domains", enabled.String())

	// if the enable-http2 flag is false (the default), http/2 should be disabled
	// due to its vulnerabilities. More specifically, disabling http/2 will
	// prevent from being vulnerable to the HTTP/2 Stream Cancellation and
	// Rapid Reset CVEs. For more information see:
	// - https://github.com/advisories/GHSA-qppj-fm5r-hxr3
	// - https://github.com/advisories/GHSA-4374-p667-p6c8
	disableHTTP2 := func(c *tls.Config) {
		setupLog.Info("Disabling HTTP/2")
		c.NextProtos = []string{"http/1.1"}
	}

	if !enableHTTP2 {
		tlsOpts = append(tlsOpts, disableHTTP2)
	}

	// Initial webhook TLS options
	webhookTLSOpts := tlsOpts
	webhookServerOptions := webhook.Options{
		TLSOpts: webhookTLSOpts,
	}

	if len(webhookCertPath) > 0 {
		setupLog.Info("Initializing webhook certificate watcher using provided certificates",
			"webhook-cert-path", webhookCertPath, "webhook-cert-name", webhookCertName, "webhook-cert-key", webhookCertKey)

		webhookServerOptions.CertDir = webhookCertPath
		webhookServerOptions.CertName = webhookCertName
		webhookServerOptions.KeyName = webhookCertKey
	}

	webhookServer := webhook.NewServer(webhookServerOptions)

	// Metrics endpoint is enabled in 'config/default/kustomization.yaml'. The Metrics options configure the server.
	// More info:
	// - https://pkg.go.dev/sigs.k8s.io/controller-runtime@v0.24.1/pkg/metrics/server
	// - https://book.kubebuilder.io/reference/metrics.html
	metricsServerOptions := metricsserver.Options{
		BindAddress:   metricsAddr,
		SecureServing: secureMetrics,
		TLSOpts:       tlsOpts,
	}

	if secureMetrics {
		// FilterProvider is used to protect the metrics endpoint with authn/authz.
		// These configurations ensure that only authorized users and service accounts
		// can access the metrics endpoint. The RBAC are configured in 'config/rbac/kustomization.yaml'. More info:
		// https://pkg.go.dev/sigs.k8s.io/controller-runtime@v0.24.1/pkg/metrics/filters#WithAuthenticationAndAuthorization
		metricsServerOptions.FilterProvider = filters.WithAuthenticationAndAuthorization
	}

	// If the certificate is not specified, controller-runtime will automatically
	// generate self-signed certificates for the metrics server. While convenient for development and testing,
	// this setup is not recommended for production.
	//
	// TODO(user): If you enable certManager, uncomment the following lines:
	// - [METRICS-WITH-CERTS] at config/default/kustomization.yaml to generate and use certificates
	// managed by cert-manager for the metrics server.
	// - [PROMETHEUS-WITH-CERTS] at config/prometheus/kustomization.yaml for TLS certification.
	if len(metricsCertPath) > 0 {
		setupLog.Info("Initializing metrics certificate watcher using provided certificates",
			"metrics-cert-path", metricsCertPath, "metrics-cert-name", metricsCertName, "metrics-cert-key", metricsCertKey)

		metricsServerOptions.CertDir = metricsCertPath
		metricsServerOptions.CertName = metricsCertName
		metricsServerOptions.KeyName = metricsCertKey
	}

	mgr, err := ctrl.NewManager(ctrl.GetConfigOrDie(), ctrl.Options{
		Scheme:                 scheme,
		Metrics:                metricsServerOptions,
		WebhookServer:          webhookServer,
		HealthProbeBindAddress: probeAddr,
		LeaderElection:         enableLeaderElection,
		LeaderElectionID:       enabled.LeaderElectionID(),
		// LeaderElectionReleaseOnCancel defines if the leader should step down voluntarily
		// when the Manager ends. This requires the binary to immediately end when the
		// Manager is stopped, otherwise, this setting is unsafe. Setting this significantly
		// speeds up voluntary leader transitions as the new leader don't have to wait
		// LeaseDuration time first.
		//
		// In the default scaffold provided, the program ends immediately after
		// the manager stops, so would be fine to enable this option. However,
		// if you are doing or is intended to do any operation such as perform cleanups
		// after the manager stops then its usage might be unsafe.
		// LeaderElectionReleaseOnCancel: true,
	})
	if err != nil {
		setupLog.Error(err, "Failed to start manager")
		os.Exit(1)
	}

	if enabled.Has(domains.VM) {
		if err := (&controller.ManagedImageReconciler{
			Client:   mgr.GetClient(),
			Scheme:   mgr.GetScheme(),
			Recorder: mgr.GetEventRecorderFor("managedimage"),
		}).SetupWithManager(mgr); err != nil {
			setupLog.Error(err, "Failed to create controller", "controller", "managedimage")
			os.Exit(1)
		}
		if err := (&controller.ManagedVMOperationReconciler{
			Client:   mgr.GetClient(),
			Scheme:   mgr.GetScheme(),
			Recorder: mgr.GetEventRecorderFor("managedvmoperation"),
		}).SetupWithManager(mgr); err != nil {
			setupLog.Error(err, "Failed to create controller", "controller", "managedvmoperation")
			os.Exit(1)
		}
		if err := (&controller.ManagedVMTemplateReconciler{
			Client:   mgr.GetClient(),
			Scheme:   mgr.GetScheme(),
			Recorder: mgr.GetEventRecorderFor("managedvmtemplate"),
		}).SetupWithManager(mgr); err != nil {
			setupLog.Error(err, "Failed to create controller", "controller", "managedvmtemplate")
			os.Exit(1)
		}
		if err := (&controller.ManagedVMReconciler{
			Client:           mgr.GetClient(),
			Scheme:           mgr.GetScheme(),
			Recorder:         mgr.GetEventRecorderFor("managedvm"),
			KubeOVNNamespace: func() string { return os.Getenv("KUBE_OVN_NAMESPACE") },
		}).SetupWithManager(mgr); err != nil {
			setupLog.Error(err, "Failed to create controller", "controller", "managedvm")
			os.Exit(1)
		}
	}
	// Webhooks belong to the same domain as the controllers they guard: a
	// process running only the network domain must not answer for VM admission,
	// or turning that domain off would leave a webhook configuration pointing
	// at a service with no endpoints and block every VM in the cluster.
	// nolint:goconst
	if enabled.Has(domains.VM) && os.Getenv("ENABLE_WEBHOOKS") != "false" {
		if err := webhookv1alpha1.SetupManagedTenantWebhookWithManager(mgr); err != nil {
			setupLog.Error(err, "Failed to create webhook", "webhook", "ManagedTenant")
			os.Exit(1)
		}
		if err := webhookv1alpha1.SetupManagedVMWebhookWithManager(mgr); err != nil {
			setupLog.Error(err, "Failed to create webhook", "webhook", "ManagedVM")
			os.Exit(1)
		}
		if err := webhookv1alpha1.SetupRawVMGuardWithManager(mgr); err != nil {
			setupLog.Error(err, "Failed to create webhook", "webhook", "RawVirtualMachineGuard")
			os.Exit(1)
		}
		// The guard admits everything when it cannot be reached, which is the
		// right trade for availability and the wrong one to leave unobserved.
		if err := webhookv1alpha1.SetupGuardWatchdogWithManager(mgr, os.Getenv("POD_NAMESPACE")); err != nil {
			setupLog.Error(err, "Failed to start the guard watchdog")
			os.Exit(1)
		}
	}
	if enabled.Has(domains.Tenant) {
		if err := (&controller.ManagedTenantReconciler{
			Client:           mgr.GetClient(),
			Scheme:           mgr.GetScheme(),
			Recorder:         mgr.GetEventRecorderFor("managedtenant"),
			APIReader:        mgr.GetAPIReader(),
			MetalLBPool:      os.Getenv("TENANTS_CP_METALLB_POOL"),
			MetalLBNamespace: os.Getenv("TENANTS_CP_METALLB_NAMESPACE"),
			TransitSubnet:    os.Getenv("TENANTS_CP_TRANSIT_SUBNET"),
			KubeOVNNamespace: os.Getenv("KUBE_OVN_NAMESPACE"),
		}).SetupWithManager(mgr); err != nil {
			setupLog.Error(err, "Failed to create controller", "controller", "managedtenant")
			os.Exit(1)
		}
	}
	if enabled.Has(domains.Tenant) {
		if err := (&controller.TalosBootstrapReconciler{
			Client:   mgr.GetClient(),
			Scheme:   mgr.GetScheme(),
			Recorder: mgr.GetEventRecorderFor("talosbootstrap"),
		}).SetupWithManager(mgr); err != nil {
			setupLog.Error(err, "Failed to create controller", "controller", "talosbootstrap")
			os.Exit(1)
		}
	}
	if enabled.Has(domains.Network) {
		if err := (&controller.AnnouncementPolicyReconciler{
			Client:   mgr.GetClient(),
			Scheme:   mgr.GetScheme(),
			Recorder: mgr.GetEventRecorderFor("announcementpolicy"),
		}).SetupWithManager(mgr); err != nil {
			setupLog.Error(err, "Failed to create controller", "controller", "announcementpolicy")
			os.Exit(1)
		}
		if err := (&controller.ManagedNetworkReconciler{
			Client:         mgr.GetClient(),
			Scheme:         mgr.GetScheme(),
			Recorder:       mgr.GetEventRecorderFor("managednetwork"),
			APIReader:      mgr.GetAPIReader(),
			ServiceCIDR:    serviceCIDRFlag,
			TenantSupernet: tenantSupernetFlag,
			MgmtCIDRs:      splitList(mgmtCIDRFlag),
			TransitSubnet:  os.Getenv("TENANTS_CP_TRANSIT_SUBNET"),
		}).SetupWithManager(mgr); err != nil {
			setupLog.Error(err, "Failed to create controller", "controller", "managednetwork")
			os.Exit(1)
		}
		if err := (&controller.ManagedNetworkPeeringReconciler{
			Client:   mgr.GetClient(),
			Scheme:   mgr.GetScheme(),
			Recorder: mgr.GetEventRecorderFor("managednetworkpeering"),
		}).SetupWithManager(mgr); err != nil {
			setupLog.Error(err, "Failed to create controller", "controller", "managednetworkpeering")
			os.Exit(1)
		}
		if err := (&controller.ManagedUnderlayReconciler{
			Client:   mgr.GetClient(),
			Scheme:   mgr.GetScheme(),
			Recorder: mgr.GetEventRecorderFor("managedunderlay"),
		}).SetupWithManager(mgr); err != nil {
			setupLog.Error(err, "Failed to create controller", "controller", "managedunderlay")
			os.Exit(1)
		}
	}
	// +kubebuilder:scaffold:builder

	if err := mgr.AddHealthzCheck("healthz", healthz.Ping); err != nil {
		setupLog.Error(err, "Failed to set up health check")
		os.Exit(1)
	}
	if err := mgr.AddReadyzCheck("readyz", healthz.Ping); err != nil {
		setupLog.Error(err, "Failed to set up ready check")
		os.Exit(1)
	}

	setupLog.Info("Starting manager")
	if err := mgr.Start(ctrl.SetupSignalHandler()); err != nil {
		setupLog.Error(err, "Failed to run manager")
		os.Exit(1)
	}
}
