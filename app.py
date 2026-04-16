import datetime
import os
import random
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from docker import DockerClient, from_env
import docker.types
import docker.errors
from arkitekt_next import background, context, easy, register, startup, progress, log
from arkitekt_next.node_id import get_or_set_node_id
from kabinet.api.schema import (
    Backend,
    Definition,
    Flavour,
    Pod,
    PodStatus,
    Release,
    Resource,
    ListFlavour,
    CudaSelector,
    update_pod,
    QualifierInput,
    list_flavours,
    FlavourFilter,
    FlavourOrder,
    Ordering,
    adeclare_backend,
    create_deployment,
    dump_logs,
    create_pod,
    aget_pod,
    my_pod_at,
    delete_pod,
    get_flavour,
    adeclare_resource,
)
from unlok_next.api.schema import (
    ManifestInput,
    RequirementInput,
    PublicSourceKind,
    PublicSourceInput,
    create_client,
    DetailClient,
)
import rekuest_next
import koil

# --- CONFIGURATION ---
ME = os.getenv("ME_ID", "FAKE GOD")
ARKITEKT_GATEWAY = os.getenv("ARKITEKT_GATEWAY", "go.arkitekt.live")
ARKITEKT_NETWORK = os.getenv("ARKITEKT_NETWORK", "next_default")


# --- HELPER FUNCTIONS ---


def _check_gpu_capability(client: DockerClient) -> bool:
    """
    The 'Definite' Route.
    Attempts to actually run a lightweight container with GPU requests.
    If it fails, the host definitively cannot handle GPU workloads.
    """
    print("Performing strict GPU hardware check...")
    try:
        # We try to run a tiny command with the GPU request
        # We use a lightweight image that is likely to exist or downloads fast
        client.containers.run(
            "alpine",
            "echo 'GPU Check'",
            remove=True,
            device_requests=[
                docker.types.DeviceRequest(count=-1, capabilities=[["gpu"]])
            ],
        )
        print("✅ GPU Hardware Check Passed.")
        return True
    except docker.errors.ImageNotFound:
        print("⚠️ Alpine image not found for check, pulling...")
        try:
            client.images.pull("alpine")
            return _check_gpu_capability(client)  # Retry once
        except:
            return False
    except Exception as e:
        print(f"❌ GPU Hardware Check Failed: {e}")
        return False


def _docker_params_from_flavour(
    flavour: ListFlavour,
) -> Dict[str, List[docker.types.DeviceRequest]]:
    docker_params: Dict[str, List[docker.types.DeviceRequest]] = {}
    for selector in flavour.selectors:
        if isinstance(selector, CudaSelector):
            docker_params.setdefault("device_requests", []).append(
                docker.types.DeviceRequest(count=-1, capabilities=[["gpu"]])
            )
    return docker_params


def _select_best_resource(context: "ArkitektContext", flavour: Flavour) -> Resource:
    """
    Selects the appropriate resource (node) based on flavour requirements.
    """
    candidates = context.resources

    # Check if flavour specifically requests a GPU
    needs_gpu = any(isinstance(s, CudaSelector) for s in flavour.selectors)

    if needs_gpu:
        # Filter for nodes that have the "gpu" qualifier set to "true"
        print([res.qualifiers for res in candidates])
        candidates = [
            res
            for res in candidates
            if any(q["key"] == "gpu" and q["value"] == "true" for q in res.qualifiers)
        ]

    if not candidates:
        # Fallback logic: if we need GPU but have none, we fail early
        if needs_gpu:
            raise Exception(
                "Flavour requires GPU, but no local GPU resources are available."
            )
        # If we didn't need GPU, but candidates is empty (rare), revert to all
        candidates = context.resources

    return random.choice(candidates)


# --- CONTEXT ---


@context
@dataclass
class ArkitektContext:
    backend: Backend
    docker: DockerClient
    instance_id: str
    has_gpu: bool = False
    device_id: Optional[str] = field(default_factory=get_or_set_node_id)
    gateway: str = field(default=ARKITEKT_GATEWAY)
    network: str = field(default=ARKITEKT_NETWORK)
    resources: List[Resource] = field(default_factory=list)


# --- STARTUP ---


@startup
async def on_startup() -> ArkitektContext:
    """
    Startup: Connects to Docker, checks hardware, and registers the node.
    """

    docker_client = from_env()
    instance_id = "default"

    # 1. THE DEFINITE GPU CHECK
    has_gpu = _check_gpu_capability(docker_client)

    # 2. Register Backend
    backend = await adeclare_backend(
        instance_id=instance_id, name="Docker", kind="docker"
    )

    # 3. Register Resource (Node) with capabilities
    resources = []

    qualifiers = []
    if has_gpu:
        qualifiers.append(QualifierInput(key="gpu", value="true"))
    else:
        qualifiers.append(QualifierInput(key="gpu", value="false"))

    print(f"Declaring Local Node with Qualifiers: {qualifiers}")

    resources.append(
        await adeclare_resource(
            local_id=f"local_docker_node",
            backend=backend.id,
            name="Local Node",
            qualifiers=qualifiers,
        )
    )

    return ArkitektContext(
        docker=docker_client,
        gateway=ARKITEKT_GATEWAY,
        network=ARKITEKT_NETWORK,
        has_gpu=has_gpu,
        backend=backend,
        instance_id=instance_id,
        resources=resources,
    )


# --- BACKGROUND MONITORING ---

pod_status_mapping = {
    "created": PodStatus.PENDING,
    "running": PodStatus.RUNNING,
    "paused": PodStatus.STOPPED,
    "restarting": PodStatus.PENDING,  # Important for UI feedback
    "removing": PodStatus.STOPPING,
    "exited": PodStatus.STOPPED,
    "dead": PodStatus.FAILED,
}


@background
def container_checker(context: ArkitektContext) -> None:
    """
    Robust polling loop to monitor container health and catch crash loops.
    """
    print("Starting container health monitor")
    pod_status_cache: Dict[str, str] = {}

    while True:
        try:
            # Get all containers (even stopped ones)
            all_containers = context.docker.containers.list(all=True)

            my_containers = [
                c for c in all_containers if c.labels.get("arkitekt.live.kabinet") == ME
            ]

            for container in my_containers:
                try:
                    # Reload to get the exact millisecond status
                    container.reload()
                except Exception:
                    continue  # Container vanished

                current_status_str = container.status
                cached_status = pod_status_cache.get(container.id)

                # Only act if status changed
                if cached_status != current_status_str:
                    try:
                        pod = my_pod_at(context.instance_id, container.id)
                    except Exception:
                        print(f"Orphaned container {container.name}. Cleaning up.")
                        try:
                            container.stop()
                            container.remove()
                        except:
                            pass
                        continue

                    new_pod_status = pod_status_mapping.get(
                        current_status_str, PodStatus.UNKOWN
                    )

                    # SPECIAL LOGIC: Crash Detection
                    if current_status_str == "restarting":
                        print(f"🔄 Pod {pod.id} is restarting (Attempting recovery)...")

                    # SPECIAL LOGIC: Failed vs Stopped
                    if current_status_str == "exited":
                        exit_code = container.attrs["State"]["ExitCode"]
                        if exit_code != 0:
                            new_pod_status = PodStatus.FAILED
                            print(f"❌ Pod {pod.id} failed (Exit Code: {exit_code})")

                    update_pod(
                        local_id=container.id,
                        status=new_pod_status,
                        instance_id=context.instance_id,
                    )

                    # Update logs on status change
                    try:
                        logs = container.logs(tail=100)
                        dump_logs(pod.id, logs.decode("utf-8"))
                    except:
                        pass

                    pod_status_cache[container.id] = current_status_str
                    print(f"State Change: {container.name} is now {new_pod_status}")

        except Exception as e:
            print("Monitor Loop Error:", e)

        koil.sleep(2)


# --- DEPLOYMENT LOGIC ---


def _internal_deploy(
    context: ArkitektContext,
    flavour: Flavour,
    deployment_id: str,
    client: DetailClient,  # Unlok Client
):
    """
    Refactored internal logic to actually start the container.
    Contains the Restart Policy and Network logic.
    """
    docker_client = context.docker
    extra_params = _docker_params_from_flavour(flavour)

    print(f"Launching container for deployment {deployment_id}")
    print(f"Restart Policy: On-Failure (Max 5 retries)")

    container = docker_client.containers.run(
        flavour.image.image_string,
        detach=True,
        # THE FIX: Restart automatically on crash, but give up after 5 tries
        restart_policy={"Name": "on-failure", "MaximumRetryCount": 5},
        labels={
            "arkitekt.live.kabinet": ME,
            "arkitekt.live.kabinet.deployment": deployment_id,
        },
        environment={
            "FAKTS_TOKEN": client.token,
            "ARKITEKT_NODE_ID": context.device_id,
        },
        command=f"arkitekt-next run prod --token {client.token} --url {context.gateway}",
        network=context.network,
        **extra_params,
    )

    return container


@register
def deploy_flavour(flavour: Flavour, context: ArkitektContext) -> Pod:
    release = flavour.release
    progress(0, "Initializing")

    # 1. Create Client
    client = create_client(
        manifest=ManifestInput(
            identifier=release.app.identifier,
            version=release.version,
            scopes=flavour.manifest["scopes"],
            requirements=[
                RequirementInput(**req.model_dump())
                for req in flavour.requirements  # TODO: The serialization here is a bit annoying
            ],
            publicSources=[
                PublicSourceInput(
                    kind=PublicSourceKind.GITHUB, url=flavour.repo.url
                )  # As currently only github is supported
            ],
            node_id=context.device_id,  # the deployed app will share the same node_id as the host (as it might spawn on different pods)
        ),
    )
    
    
    print(f"Client created for {release.app.identifier}:{release.version} with scopes {flavour.manifest['scopes']}")

    # 2. Pull Image (Simplified logic for brevity, assuming standard pull works)
    progress(10, "Pulling image...")
    try:
        context.docker.images.pull(
            flavour.image.image_string
        )  # Pull the image (maybe print progress in real implementation)
    except Exception as e:
        print(f"Pull error: {e}")
    progress(60, "Image Pulled")

    # 3. Create Deployment Record
    deployment = create_deployment(
        flavour=flavour,
        instance_id=context.instance_id,
        local_id=flavour.image.image_string,
        last_pulled=datetime.datetime.now(),
    )

    # 4. Launch Container (Using shared logic)
    progress(70, "Starting Container")
    container = _internal_deploy(context, flavour, deployment.id, client)

    print(f"Deployed: {container.name} [{container.id[:10]}]")
    progress(90, "Registering Pod")

    # 5. Select Resource and Create Pod
    resource = _select_best_resource(context, flavour)

    pod = create_pod(
        deployment=deployment,
        instance_id=context.instance_id,
        local_id=container.id,
        client_id=client.oauth2_client.client_id,
        resource=resource,
    )

    return pod


@register
def deploy(release: Release, context: ArkitektContext) -> Pod:
    # Wrapper for deploy_flavour logic to handle pure Release objects
    # This automatically picks the newest/best flavour in real implementation
    # For now, we pick index 0 as per your original script
    if not release.flavours:
        raise Exception("No flavours available in this release")
    prefered_flavour = release.flavours[0]

    expanded_flavour = get_flavour(prefered_flavour.id)

    return deploy_flavour(expanded_flavour, context)


@register
def auto_install(context: ArkitektContext, definition: Definition) -> Pod:
    flavours = list_flavours(
        filters=FlavourFilter(hasDefinitions=[definition.id]),
        order=FlavourOrder(releasedAt=Ordering.DESC),
    )

    prefered_flavour = None

    for flavour in flavours:
        try:
            _select_best_resource(context, flavour)
            prefered_flavour = flavour
            break
        except Exception as e:
            print(f"Flavour {flavour.id} not suitable: {e}")
            continue

    if prefered_flavour is None:
        raise Exception("No suitable flavours available for this release on this node")

    expanded_flavour = get_flavour(prefered_flavour.id)

    return deploy_flavour(expanded_flavour, context)


# --- LIFECYCLE MANAGEMENT ---


@register
def refresh_logs(context: ArkitektContext, pod: Pod) -> Pod:
    try:
        container = context.docker.containers.get(
            pod.local_id
        )  # Use local_id for reliability
        logs = container.logs(tail=200)
        dump_logs(pod.id, logs.decode("utf-8"))
    except Exception as e:
        print(f"Error refreshing logs: {e}")
    return pod


@register
def restart(pod: Pod, context: ArkitektContext) -> Pod:
    print(f"Restarting {pod.id}")
    container = context.docker.containers.get(pod.pod_id)
    container.restart()
    return pod


@register
def stop(pod: Pod, context: ArkitektContext) -> Pod:
    print(f"Stopping {pod.id}")
    container = context.docker.containers.get(pod.pod_id)
    # This acts as the "Tell it otherwise" command.
    # The 'on-failure' policy only restarts on crash. 'stop' is a valid exit.
    container.stop()
    return pod


@register
def remove(pod: Pod, context: ArkitektContext) -> Pod:
    print(f"Removing {pod.id}")
    try:
        container = context.docker.containers.get(pod.pod_id)
        container.remove(force=True)
    except Exception as e:
        print(f"Container removal error (might already be gone): {e}")

    delete_pod(pod.id)
    return pod


@register
def move(pod: Pod, target: Resource) -> Pod:
    # Simplistic implementation: Update DB record.
    # In reality, you cannot "move" a Docker container between hosts without restarting/redeploying.
    print(f"Updating Pod record {pod.id} to resource {target.id}")
    pod.resource_id = target.id
    return pod
