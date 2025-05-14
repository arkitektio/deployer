import datetime
import os
import random
import time
from dataclasses import dataclass, field
from typing import  Dict, List
from docker import DockerClient, from_env
import docker.types
from arkitekt_next import background, context, easy, register, startup, progress, log
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
    adeclare_backend,
    create_deployment,
    dump_logs,
    create_pod,
    delete_pod,
    get_flavour,
    adeclare_resource,
)
from api.kabinet import  get_detail_definition
from unlok_next.api.schema import (
    ManifestInput,
    Requirement,
    create_client,
)

# Connect to local Dockers

ME = os.getenv("INSTANCE_ID", "FAKE GOD")
ARKITEKT_GATEWAY = os.getenv("ARKITEKT_GATEWAY", "caddy")
ARKITEKT_NETWORK = os.getenv("ARKITEKT_NETWORK", "next_default")


def _docker_params_from_flavour(flavour: ListFlavour) -> Dict[str, List[docker.types.DeviceRequest]]:

    docker_params: Dict[str, List[docker.types.DeviceRequest]] = {}

    for selector in flavour.selectors:

        if isinstance(selector, CudaSelector):
            docker_params.setdefault("device_requests", []).append(
                docker.types.DeviceRequest(count=-1, capabilities=[["gpu"]])
            )


    return docker_params



@context
@dataclass
class ArkitektContext:
    backend: Backend
    docker: DockerClient
    instance_id: str
    gateway: str = field(default=ARKITEKT_GATEWAY)
    network: str = field(default=ARKITEKT_NETWORK)
    resources: List[Resource] = field(default_factory=list)


@startup
async def on_startup(instance_id: str) -> ArkitektContext:
    """ A startup function that runs when the actor starts up."""
    print("Starting up")
    print("Check sfosr scontainers that are no longer pods?")

    x = await adeclare_backend(instance_id=instance_id, name="Docker", kind="apptainer")

    resources = []
    for i in range(1):
        print("Creating containers")
        resources.append(
            await adeclare_resource(
                local_id=f"node_id{i}", backend=x.id, name=f"Node {i}"
            )
        )

    return ArkitektContext(
        docker=from_env(),
        gateway=ARKITEKT_GATEWAY,
        network=ARKITEKT_NETWORK,
        backend=x,
        instance_id=instance_id,
        resources=resources,
    )


@background
def container_checker(context: ArkitektContext) -> None:
    """ A background function that runs in the background.

    It checks for containers that are no longer pods and updates their status.
    If a container is no longer a pod, it stops and removes it, to ensure that the pod is not running anymore.
    

    """
    print("Starting dup")
    print("Check for containers that are dno longer pods?")

    pod_status: Dict[str, PodStatus] = {}

    while True:
        docker = context.docker

        my_containers = []
        container = docker.containers.list(all=True)
        for c in container:
            if "arkitekt.live.kabinet" in c.labels:
                classifier = c.labels["arkitekt.live.kabinet"]
                if classifier != ME:
                    continue
                my_containers.append(c)

        for container in my_containers:
            try:
                old_status = pod_status.get(container.id, None)
                if container.status != old_status:
                    p = update_pod(
                        local_id=container.id,
                        status=(
                            PodStatus.RUNNING
                            if container.status == "running"
                            else PodStatus.STOPPED
                        ),
                        instance_id=context.instance_id,
                    )

                    pod_status[container.id] = container.status
                    print("Updated Container Status")

                    logs = container.logs(tail=60)
                    dump_logs(p.id, logs.decode("utf-8"))
            except Exception as e:
                print("Error updating pod status", e)
                container.stop()
                container.remove()

        time.sleep(10)


@register
def refresh_logs(context: ArkitektContext, pod: Pod) -> Pod:
    """ Refresh Logs

    Refreshes the logs of a pod by getting the logs from the container and updating the logs of the pod.

    Parameters
    ----------

    context: ArkitektContext
        The context of the current instance

    pod: Pod
        The pod to refresh the logs for

    Returns
    -------

    Pod
    The pod with the updated logs

    """
    print(pod.pod_id)
    print(context.docker.containers.list())
    container = context.docker.containers.get(pod.pod_id)
    print("Getting logs")
    logs = container.logs(tail=60)
    print("Dumping logs")
    dump_logs(pod.id, logs.decode("utf-8"))

    return pod


@register
def restart(pod: Pod, context: ArkitektContext) -> Pod:
    """Restart

    Restarts a pod by stopping and starting it again.


    """

    print("Running")
    container = context.docker.containers.get(pod.pod_id)

    progress(50)
    container.restart()
    progress(100)
    return pod


@register
def stop(pod: Pod, context: ArkitektContext) -> Pod:
    """Stop

    Stops a pod by stopping and does not start it again.


    """

    print("Running")
    container = context.docker.containers.get(pod.pod_id)

    container.stop()

    return pod


@register
def remove(pod: Pod, context: ArkitektContext) -> Pod:
    """Remove

    Remove a pod by stopping and removing it.
    This pod will not be able to be started again.


    """

    print("Running")
    try:
        container = context.docker.containers.get(pod.pod_id)

        container.remove()
    except Exception as e:
        log(e)
        print(e)

    delete_pod(pod.id)

    return pod


@register
def deploy_flavour(flavour: Flavour, context: ArkitektContext) -> Pod:
    """Deploy Flavour

    Deploys a specific flavour on the current docker instance.


    Parameters
    ----------

    release: Release
        The release to deploy

    context: ArkitektContext
        The context of the current instance

    
    Returns
    -------

    Pod
        The pod that was deployed

    """


    docker: DockerClient = context.docker
    caddy_url = context.gateway
    network = context.network


    release = flavour.release

    progress(0)

    print(flavour.requirements)

    print(
        [Requirement(**req.model_dump()) for req in flavour.requirements]
    )

    client = create_client(
        manifest=ManifestInput(
                identifier=release.app.identifier,
                version=release.version,
                scopes=flavour.manifest["scopes"],
        ),
        requirements=[Requirement(**req.model_dump()) for req in flavour.requirements],
        
    )

    # Track progress of each layer
    layer_progress = {}
    for line in docker.api.pull(flavour.image.image_string, stream=True, decode=True):
        if 'id' in line and 'progress' in line:
            # Update progress based on pull status
            progress_detail = line.get('progressDetail', {})
            if progress_detail and 'current' in progress_detail and 'total' in progress_detail:
                layer_id = line['id']
                layer_progress[layer_id] = (progress_detail['current'] / progress_detail['total']) * 100
                avg_progress = int(sum(layer_progress.values()) / len(layer_progress)) // 2
                progress(avg_progress, f"Pulling: {line.get('status', '')} - {line.get('progress', '')}")
        elif 'status' in line:
            progress(30, f"Status: {line['status']}")


    progress(60, "Pulled image")

    deployment = create_deployment(
        flavour=flavour,
        instance_id=context.instance_id,
        local_id=flavour.image.image_string,
        last_pulled=datetime.datetime.now(),
    )

    progress(70, "Starting container")


    print(os.getenv("ARKITEKT_GATEWAY"))

    # COnver step here for apptainer

    extra_params = _docker_params_from_flavour(flavour)

    print(extra_params)



    container = docker.containers.run(
        flavour.image.image_string,
        detach=True,
        labels={
            "arkitekt.live.kabinet": ME,
            "arkitekt.live.kabinet.deployment": deployment.id,
        },
        environment={"FAKTS_TOKEN": client.token},
        command=f"arkitekt-next run prod --token {client.token} --url {caddy_url}",
        network=network,
        **extra_params
    )

    print(
        "Deployed container on network",
        network,
        client.token,
        caddy_url,
        container.name,
    )

    progress(90)

    resource = random.choice(context.resources)

    z = create_pod(
        deployment=deployment,
        instance_id=context.instance_id,
        local_id=container.id,
        client_id=client.oauth2_client.client_id,
        resource=resource,
    )

    return z


@register
def deploy(release: Release, context: ArkitektContext) -> Pod:
    """Deploy

    Deploys a release to the current docker instance.


    Parameters
    ----------

    release: Release
        The release to deploy

    context: ArkitektContext
        The context of the current instance

    
    Returns
    -------

    Pod
        The pod that was deployed

    """


    print(release)
    docker: DockerClient = context.docker
    caddy_url = context.gateway
    network = context.network

    flavour = release.flavours[0]

    progress(0)

    print(flavour.requirements)

    print(
        [Requirement(**req.model_dump()) for req in flavour.requirements]
    )

    client = create_client(
            manifest=ManifestInput(
                identifier=release.app.identifier,
                version=release.version,
                scopes=flavour.manifest["scopes"],
            ),
            requirements=[Requirement(**req.model_dump()) for req in flavour.requirements],
    )

    # Track progress of each layer and global GB info
    layer_progress = {}
    last_update = time.time()
    current_status = ""
    status_updates = 5
    
    progress(10, "Pulling image")
    for line in docker.api.pull(flavour.image.image_string, stream=True, decode=True):
        if 'id' in line and 'progress' in line:
            # Update progress based on pull status
            progress_detail = line.get('progressDetail', {})
            if progress_detail and 'current' in progress_detail and 'total' in progress_detail:
                layer_id = line['id']
                layer_progress[layer_id] = (progress_detail['current'] / progress_detail['total']) * 100
                avg_progress = int(sum(layer_progress.values()) / len(layer_progress)) // 2
                
                # Extract GB info without progress symbols
                progress_str = line.get('progress', '')
                progress_str = progress_str.replace('\[', '').replace('>', '').replace('\]', '').replace('=', '')
                gb_info = ' '.join(word for word in progress_str.split() if 'GB' in word)
                
                current_status = f"Pulling: {line.get('status', '')}"
                if gb_info:
                    current_status += f" - {gb_info}"
                
                # Only update progress every 10 seconds
                if time.time() - last_update >= status_updates:
                    progress(avg_progress, current_status)
                    last_update = time.time()
                    
        elif 'status' in line:
            current_status = f"Status: {line['status']}"
            if time.time() - last_update >= status_updates:
                progress(30, current_status)
                last_update = time.time()

    progress(60, "Pulled image")
    progress(60, "Pulled image")

    deployment = create_deployment(
        flavour=flavour,
        instance_id=context.instance_id,
        local_id=flavour.image.image_string,
        last_pulled=datetime.datetime.now(),
    )

    progress(70, "Starting container")


    print(os.getenv("ARKITEKT_GATEWAY"))

    # COnver step here for apptainer

    extra_params = _docker_params_from_flavour(flavour)

    print(extra_params)


    container = docker.containers.run(
        flavour.image.image_string,
        detach=True,
        labels={
            "arkitekt.live.kabinet": ME,
            "arkitekt.live.kabinet.deployment": deployment.id,
        },
        environment={"FAKTS_TOKEN": client.token},
        command=f"arkitekt-next run prod --token {client.token} --url {caddy_url}",
        network=network,
        **extra_params
        
    )

    print(
        "Deployed container on network",
        network,
        client.token,
        caddy_url,
        container.name,
    )

    progress(90)

    resource = random.choice(context.resources)

    z = create_pod(
        deployment=deployment,
        instance_id=context.instance_id,
        local_id=container.id,
        client_id=client.oauth2_client.client_id,
        resource=resource,
    )

    return z



@register
def deploy_definition(definition: Definition, context: ArkitektContext) -> Pod:
    """Deploy

    Deploys a definition to the current docker instance.

    Parameters
    ----------

    definition: Definition
        The definition to deploy

    context: ArkitektContext
        The context of the current instance

    
    Returns
    -------

    Pod
        The pod that was deployed

    """
    
    detail = get_detail_definition(definition.id)
    
    
    print(detail)
    
    selected_flavour = get_flavour(detail.flavours[0])
    
    return deploy_flavour(selected_flavour, context)



