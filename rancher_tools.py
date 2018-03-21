#!/usr/bin/env python3.6
import json
from os import environ as env
from os.path import expanduser
from time import sleep
from copy import deepcopy
from datetime import datetime, timedelta

import requests
from requests.compat import urljoin


try:
    with open(expanduser('~/.rancher/cli.json')) as f:
        CATTLE_CONFIG = json.load(f)
        CATTLE_URL = urljoin(CATTLE_CONFIG['url'], '/v2-beta')
        AUTH = (CATTLE_CONFIG['accessKey'], CATTLE_CONFIG['secretKey'])
except OSError as exc:
    print(exc)
    CATTLE_URL = env['CATTLE_URL']
    AUTH = (env['CATTLE_ACCESS_KEY'], env['CATTLE_SECRET_KEY'])

# Make sure we've got a trailing slash
CATTLE_URL = CATTLE_URL.rstrip('/') + '/'

print(f'Using cattle url: {CATTLE_URL!r}')


class TimeoutException(Exception):
    pass


class ServiceNotFoundException(Exception):
    pass


class StackNotFoundException(Exception):
    pass


def get_svc(project_id, service_id):
    """
    Gets the JSON representation of a service in a project.
    """
    resp = requests.get(
        f'{CATTLE_URL}projects/{project_id}/services/{service_id}',
        auth=AUTH
    )
    resp.raise_for_status()
    return resp.json()


def get_stack_by_name(project_id, name):
    """
    Gets a stack by its name.
    """
    resp = requests.get(
        f'{CATTLE_URL}projects/{project_id}/stacks',
        auth=AUTH,
        params=dict(name=name)
    )
    resp.raise_for_status()
    for stack in resp.json()['data']:
        if stack['name'] == name:
            return stack
    raise StackNotFoundException()


def get_svc_by_stack_and_name(stack, name):
    """
    Gets the service by stack and name.
    """
    resp = requests.get(
        stack['links']['services'],
        auth=AUTH,
        params=dict(name=name)
    )
    resp.raise_for_status()
    for svc in resp.json()['data']:
        if svc['name'] == name:
            return svc
    raise ServiceNotFoundException()


def rename_svc(svc, new_name):
    resp = requests.put(
        svc['links']['self'],
        auth=AUTH,
        json=dict(name=new_name)
    )
    resp.raise_for_status()
    return resp.json()


def refresh_svc(svc):
    """
    Fetches the latest service data from Rancher.
    """
    return get_svc(*svc_ids(svc))


def svc_ids(svc):
    """
    Returns the account ID and service ID of a given service.
    """
    return svc['accountId'], svc['id']


def await_active(svc, timeout=None):
    """
    Blocks until the service status becomes 'active'. Takes a timeout in
    seconds, after which we will give up waiting and raise an exception.
    """
    if timeout is not None:
        deadline = datetime.now() + timedelta(seconds=timeout)
    else:
        deadline = datetime.max
    while svc['state'] != 'active':
        if datetime.now() > deadline:
            raise TimeoutException()
        sleep(1)
        svc = refresh_svc(svc)
    return svc


def await_healthy(svc, timeout=None):
    """
    Blocks until the service status becomes 'healthy'. Takes a timeout in
    seconds, after which we will give up waiting and raise an exception.
    """
    if timeout is not None:
        deadline = datetime.now() + timedelta(seconds=timeout)
    else:
        deadline = datetime.max
    while svc['healthState'] != 'healthy':
        if datetime.now() > deadline:
            raise TimeoutException()
        sleep(1)
        svc = refresh_svc(svc)
    return svc


def get_lb_svc_target(lb_svc, source_port, path):
    """
    Gets the service which is targetted by the load balancer rule which
    matches the source_port and path provided.
    """
    project_id, _ = svc_ids(lb_svc)
    for pr in lb_svc['lbConfig']['portRules']:
        if pr['sourcePort'] == source_port and pr.get('path') == path:
            return get_svc(project_id, pr['serviceId'])
    raise ServiceNotFoundException()


def change_lb_svc_target(lb_svc, source_port, path, target_svc_id):
    """
    Change the service target of a load balancer rule which matches the
    source_port and path provided.
    """
    lb_svc = deepcopy(lb_svc)
    lb_config = lb_svc['lbConfig']
    changed = False
    for pr in lb_config['portRules']:
        if pr['sourcePort'] == source_port and pr.get('path') == path:
            pr['serviceId'] = target_svc_id
            changed = True
            break
    if not changed:
        raise ValueError(
            f'Port rule with source_port {source_port!r} '
            f'and path {path!r} not found.')
    resp = requests.put(
        lb_svc['links']['self'],
        auth=AUTH,
        json=dict(lbConfig=lb_config)
    )
    resp.raise_for_status()
    return resp.json()


def finish_any_previous_upgrade(svc):
    """
    If service has been previously upgraded, tell Rancher to finish the upgrade
    """
    if svc['state'] != 'upgraded':
        return svc
    project_id, service_id = svc_ids(svc)
    resp = requests.post(
        f'{CATTLE_URL}projects/{project_id}/services/{service_id}',
        auth=AUTH,
        params=dict(action='finishupgrade')
    )
    resp.raise_for_status()
    return resp.json()


def create_svc(
        project_id, stack_id, name, image_name, config=None, launch_config=None):
    """
    Create a brand new service.
    """
    # Set defaults
    _config = dict(
        scale=1,
        startOnCreate=True
    )
    _launch_config = dict(
        tty=True
    )

    # Add any provided config
    if config is not None:
        _config.update(config)
    if launch_config is not None:
        _launch_config.update(launch_config)

    # Overwrite any config with provided required values
    _config.update(dict(
        type='service',
        name=name,
        stackId=stack_id,
        launchConfig=_launch_config
    ))
    _launch_config.update(dict(
        imageUuid=f'docker:{image_name}'
    ))

    resp = requests.post(
        f'{CATTLE_URL}projects/{project_id}/services',
        auth=AUTH,
        json=_config
    )
    resp.raise_for_status()
    return resp.json()


def clone_svc(svc, new_name, new_image=None, config=None, launch_config=None):
    """
    Clone a service and optionally bring the clone up with a new image.
    """
    svc = deepcopy(svc)
    project_id, _ = svc_ids(svc)

    svc.update(config or {})
    svc['launchConfig'].update(launch_config or {})
    if new_image is not None:
        svc['launchConfig']['imageUuid'] = f'docker:{new_image}'
    svc['name'] = new_name

    resp = requests.post(
        f'{CATTLE_URL}projects/{project_id}/services',
        auth=AUTH,
        json=svc
    )
    resp.raise_for_status()
    return resp.json()


def upgrade_svc_images(svc, new_image=None, new_secondary_images=None):
    """
    Upgrade a service to use a new image. Also upgrades sidekick services.
    """
    svc = deepcopy(svc)
    svc = finish_any_previous_upgrade(svc)
    svc = await_active(svc)
    project_id, service_id = svc_ids(svc)

    launch_config = svc['launchConfig']
    slcs = {x['name']: x for x in svc.get('secondaryLaunchConfigs', [])}

    if new_image is not None:
        launch_config['imageUuid'] = f'docker:{new_image}'

    if new_secondary_images is not None:
        for name, img in new_secondary_images.items():
            slcs[name]['imageUuid'] = f'docker:{img}'

    resp = requests.post(
        f'{CATTLE_URL}projects/{project_id}/services/{service_id}',
        auth=AUTH,
        params=dict(action='upgrade'),
        json=dict(inServiceStrategy=dict(
            launchConfig=launch_config,
            secondaryLaunchConfigs=list(slcs.values())
        ))
    )
    resp.raise_for_status()
    return resp.json()


def restart_svc(svc, batch_size=1, interval=1000):
    """
    Restart a service.
    """
    project_id, service_id = svc_ids(svc)
    resp = requests.post(
        f'{CATTLE_URL}projects/{project_id}/services/{service_id}',
        auth=AUTH,
        params=dict(action='restart'),
        json=dict(rollingRestartStrategy=dict(
            batchSize=batch_size,
            intervalMillis=interval
        ))
    )
    resp.raise_for_status()
    return resp.json()
