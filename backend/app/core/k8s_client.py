"""Kubernetes client wrapper using kubernetes-asyncio."""

import logging
import os
from typing import Any

from kubernetes_asyncio import client, config
from kubernetes_asyncio.client import ApiClient, ApiException, AppsV1Api, CustomObjectsApi, CoreV1Api

from app.config import get_settings
from app.core.constants import KUBEVIRT_API_GROUP, KUBEVIRT_API_VERSION, CDI_API_GROUP, CDI_API_VERSION

logger = logging.getLogger(__name__)
settings = get_settings()


class K8sClient:
    """Async Kubernetes client wrapper."""

    def __init__(self) -> None:
        self._api_client: ApiClient | None = None
        self._custom_api: CustomObjectsApi | None = None
        self._core_api: CoreV1Api | None = None
        self._apps_api: AppsV1Api | None = None
        self._initialized = False

    async def initialize(self) -> None:
        """Initialize Kubernetes client configuration."""
        if self._initialized:
            return

        kubeconfig_path = settings.kubeconfig_path

        try:
            if kubeconfig_path and os.path.exists(kubeconfig_path):
                logger.info(f"Loading kubeconfig from {kubeconfig_path}")
                await config.load_kube_config(config_file=kubeconfig_path)
            elif settings.k8s_in_cluster or os.path.exists(
                "/var/run/secrets/kubernetes.io/serviceaccount/token"
            ):
                logger.info("Loading in-cluster config")
                config.load_incluster_config()
            else:
                # Try default kubeconfig location
                logger.info("Loading default kubeconfig")
                await config.load_kube_config()

            self._api_client = ApiClient()
            self._custom_api = CustomObjectsApi(self._api_client)
            self._core_api = CoreV1Api(self._api_client)
            self._apps_api = AppsV1Api(self._api_client)
            self._initialized = True
            logger.info("Kubernetes client initialized successfully")

        except Exception as e:
            logger.error(f"Failed to initialize Kubernetes client: {e}")
            raise

    async def close(self) -> None:
        """Close the API client."""
        if self._api_client:
            await self._api_client.close()
            self._initialized = False

    @property
    def custom_api(self) -> CustomObjectsApi:
        """Get CustomObjectsApi instance."""
        if not self._custom_api:
            raise RuntimeError("Kubernetes client not initialized")
        return self._custom_api

    @property
    def core_api(self) -> CoreV1Api:
        """Get CoreV1Api instance."""
        if not self._core_api:
            raise RuntimeError("Kubernetes client not initialized")
        return self._core_api

    @property
    def apps_api(self) -> AppsV1Api:
        """Get AppsV1Api instance."""
        if not self._apps_api:
            raise RuntimeError("Kubernetes client not initialized")
        return self._apps_api

    # =========================================================================
    # Virtual Machines
    # =========================================================================

    async def list_virtual_machines(
        self, namespace: str | None = None
    ) -> list[dict[str, Any]]:
        """List VirtualMachines in namespace or all namespaces."""
        try:
            if namespace:
                result = await self.custom_api.list_namespaced_custom_object(
                    group=KUBEVIRT_API_GROUP,
                    version=KUBEVIRT_API_VERSION,
                    namespace=namespace,
                    plural="virtualmachines",
                )
            else:
                result = await self.custom_api.list_cluster_custom_object(
                    group=KUBEVIRT_API_GROUP,
                    version=KUBEVIRT_API_VERSION,
                    plural="virtualmachines",
                )
            return result.get("items", [])
        except ApiException as e:
            logger.error(f"Failed to list VMs: {e}")
            raise

    async def get_virtual_machine(self, name: str, namespace: str) -> dict[str, Any]:
        """Get a specific VirtualMachine."""
        try:
            return await self.custom_api.get_namespaced_custom_object(
                group=KUBEVIRT_API_GROUP,
                version=KUBEVIRT_API_VERSION,
                namespace=namespace,
                plural="virtualmachines",
                name=name,
            )
        except ApiException as e:
            logger.error(f"Failed to get VM {name}: {e}")
            raise

    async def create_virtual_machine(
        self, namespace: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        """Create a VirtualMachine."""
        try:
            return await self.custom_api.create_namespaced_custom_object(
                group=KUBEVIRT_API_GROUP,
                version=KUBEVIRT_API_VERSION,
                namespace=namespace,
                plural="virtualmachines",
                body=body,
            )
        except ApiException as e:
            logger.error(f"Failed to create VM: {e}")
            raise

    async def patch_virtual_machine(
        self, name: str, namespace: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        """Patch a VirtualMachine using merge-patch."""
        try:
            return await self.custom_api.patch_namespaced_custom_object(
                group=KUBEVIRT_API_GROUP,
                version=KUBEVIRT_API_VERSION,
                namespace=namespace,
                plural="virtualmachines",
                name=name,
                body=body,
                _content_type="application/merge-patch+json",
            )
        except ApiException as e:
            logger.error(f"Failed to patch VM {name}: {e}")
            raise

    async def delete_virtual_machine(self, name: str, namespace: str) -> dict[str, Any]:
        """Delete a VirtualMachine."""
        try:
            return await self.custom_api.delete_namespaced_custom_object(
                group=KUBEVIRT_API_GROUP,
                version=KUBEVIRT_API_VERSION,
                namespace=namespace,
                plural="virtualmachines",
                name=name,
            )
        except ApiException as e:
            logger.error(f"Failed to delete VM {name}: {e}")
            raise

    # =========================================================================
    # Virtual Machine Instances
    # =========================================================================

    async def list_virtual_machine_instances(
        self, namespace: str | None = None
    ) -> list[dict[str, Any]]:
        """List VirtualMachineInstances."""
        try:
            if namespace:
                result = await self.custom_api.list_namespaced_custom_object(
                    group=KUBEVIRT_API_GROUP,
                    version=KUBEVIRT_API_VERSION,
                    namespace=namespace,
                    plural="virtualmachineinstances",
                )
            else:
                result = await self.custom_api.list_cluster_custom_object(
                    group=KUBEVIRT_API_GROUP,
                    version=KUBEVIRT_API_VERSION,
                    plural="virtualmachineinstances",
                )
            return result.get("items", [])
        except ApiException as e:
            logger.error(f"Failed to list VMIs: {e}")
            raise

    async def get_virtual_machine_instance(
        self, name: str, namespace: str
    ) -> dict[str, Any]:
        """Get a specific VirtualMachineInstance."""
        try:
            return await self.custom_api.get_namespaced_custom_object(
                group=KUBEVIRT_API_GROUP,
                version=KUBEVIRT_API_VERSION,
                namespace=namespace,
                plural="virtualmachineinstances",
                name=name,
            )
        except ApiException as e:
            logger.error(f"Failed to get VMI {name}: {e}")
            raise

    async def delete_virtual_machine_instance(
        self, name: str, namespace: str
    ) -> dict[str, Any]:
        """Delete a VirtualMachineInstance (for restart)."""
        try:
            return await self.custom_api.delete_namespaced_custom_object(
                group=KUBEVIRT_API_GROUP,
                version=KUBEVIRT_API_VERSION,
                namespace=namespace,
                plural="virtualmachineinstances",
                name=name,
            )
        except ApiException as e:
            logger.error(f"Failed to delete VMI {name}: {e}")
            raise

    # =========================================================================
    # Namespaces
    # =========================================================================

    async def list_namespaces(
        self, label_selector: str | None = None, field_selector: str | None = None,
    ) -> list[dict[str, Any]]:
        """List namespaces, optionally filtered by label/field selector."""
        try:
            kwargs: dict[str, str] = {}
            if label_selector:
                kwargs["label_selector"] = label_selector
            if field_selector:
                kwargs["field_selector"] = field_selector
            result = await self.core_api.list_namespace(**kwargs)
            return [
                {
                    "name": ns.metadata.name,
                    "status": ns.status.phase,
                    "labels": ns.metadata.labels or {},
                    "created": ns.metadata.creation_timestamp.isoformat()
                    if ns.metadata.creation_timestamp
                    else None,
                }
                for ns in result.items
            ]
        except ApiException as e:
            logger.error(f"Failed to list namespaces: {e}")
            raise

    # =========================================================================
    # Nodes
    # =========================================================================

    async def list_nodes(self) -> list[dict[str, Any]]:
        """List all nodes."""
        try:
            result = await self.core_api.list_node()
            nodes = []
            for node in result.items:
                # Get node status
                conditions = {c.type: c.status for c in node.status.conditions or []}
                ready = conditions.get("Ready", "Unknown")

                # Get allocatable resources
                allocatable = node.status.allocatable or {}

                nodes.append(
                    {
                        "name": node.metadata.name,
                        "status": "Ready" if ready == "True" else "NotReady",
                        "roles": [
                            k.replace("node-role.kubernetes.io/", "")
                            for k in (node.metadata.labels or {}).keys()
                            if k.startswith("node-role.kubernetes.io/")
                        ],
                        "version": node.status.node_info.kubelet_version
                        if node.status.node_info
                        else None,
                        "os": node.status.node_info.os_image
                        if node.status.node_info
                        else None,
                        "cpu": allocatable.get("cpu"),
                        "memory": allocatable.get("memory"),
                        "internal_ip": next(
                            (
                                addr.address
                                for addr in (node.status.addresses or [])
                                if addr.type == "InternalIP"
                            ),
                            None,
                        ),
                    }
                )
            return nodes
        except ApiException as e:
            logger.error(f"Failed to list nodes: {e}")
            raise

    # =========================================================================
    # Cluster Status
    # =========================================================================

    async def get_kubevirt_status(self) -> dict[str, Any]:
        """Get KubeVirt deployment status."""
        try:
            result = await self.custom_api.list_cluster_custom_object(
                group=KUBEVIRT_API_GROUP,
                version=KUBEVIRT_API_VERSION,
                plural="kubevirts",
            )
            items = result.get("items", [])
            if items:
                kv = items[0]
                return {
                    "installed": True,
                    "phase": kv.get("status", {}).get("phase"),
                    "version": kv.get("status", {}).get("observedKubeVirtVersion"),
                    "targetVersion": kv.get("status", {}).get("targetKubeVirtVersion"),
                }
            return {"installed": False}
        except ApiException as e:
            logger.error(f"Failed to get KubeVirt status: {e}")
            return {"installed": False, "error": str(e)}

    async def get_cdi_status(self) -> dict[str, Any]:
        """Get CDI deployment status."""
        try:
            result = await self.custom_api.list_cluster_custom_object(
                group=CDI_API_GROUP,
                version=CDI_API_VERSION,
                plural="cdis",
            )
            items = result.get("items", [])
            if items:
                cdi = items[0]
                return {
                    "installed": True,
                    "phase": cdi.get("status", {}).get("phase"),
                }
            return {"installed": False}
        except ApiException as e:
            logger.error(f"Failed to get CDI status: {e}")
            return {"installed": False, "error": str(e)}

    # =========================================================================
    # Health Check
    # =========================================================================

    async def check_connectivity(self) -> bool:
        """Check if we can connect to the Kubernetes API."""
        try:
            await self.core_api.get_api_resources()
            return True
        except Exception as e:
            logger.error(f"Kubernetes API connectivity check failed: {e}")
            return False
