#!/usr/bin/env python3
# -*- coding: utf-8 -*-
""" A docker swarm service for automatically updating your services to the latest image tag push. """

import logging
import pause
import docker
from prometheus_client import start_http_server
from .lib import constants
from .lib import prometheus
from .notifiers import core

log = logging.getLogger('cioban')


class Cioban():
    """ The main class """
    settings = {
        'filters': {},
        'blacklist': {},
        'sleep_time': '5m',
        'prometheus_port': 9308,
        'notifiers': [],
        'telegram_chat_id': None,
        'telegram_token': None,
        'notify_include_new_image': False,
        'notify_include_old_image': False,
    }
    docker = docker.from_env()
    notifiers = core.start()

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            if k in self.settings:
                self.settings[k] = v
            else:
                log.debug(f'{k} not found in settings')

        prometheus.PROM_INFO.info({'version': f'{constants.VERSION}'})

        relation = {
            's': 'seconds',
            'm': 'minutes',
            'h': 'hours',
            'd': 'days',
            'w': 'weeks',
        }
        if any(s.isalpha() for s in self.settings['sleep_time']):
            try:
                self.sleep = int(self.settings['sleep_time'][:-1])
            except ValueError:
                raise ValueError(f"{self.settings['sleep_time']} not understood")

            if self.settings['sleep_time'][-1] in relation:
                self.sleep_type = relation[self.settings['sleep_time'][-1]]
            else:
                raise ValueError(f"{self.settings['sleep_time']} not understood")
        else:
            self.sleep = int(self.settings['sleep_time'])
            self.sleep_type = 'minutes'

        if self.settings.get('notifiers'):
            for notifier in self.settings['notifiers']:
                notifier_options = {}
                for k, v in kwargs.items():
                    if notifier.lower() in k.lower():
                        notifier_options.update({k.lower(): v})
                self.notifiers.register(notifier, **notifier_options)
                log.debug('Registered {}'.format(notifier))

        log.debug('Cioban initialized')

    def run(self):
        """ prepares the run and then triggers it. this is the actual loop """
        start_http_server(self.settings['prometheus_port'])  # starts the prometheus metrics server
        while True:
            prometheus.PROM_STATE_ENUM.state('running')
            log.info('Starting update run')
            self._run()
            log.info(f'Sleeping for {self.sleep} {self.sleep_type}')
            prometheus.PROM_STATE_ENUM.state('sleeping')
            wait = getattr(pause, self.sleep_type)
            wait(self.sleep)

    def __get_updated_image(self, image, image_sha):
        """ checks if an image has an update """
        registry_data = None
        updated_image = None
        try:
            registry_data = self.docker.images.get_registry_data(image)
        except docker.errors.APIError as error:
            log.error(f'Failed to retrieve the registry data for {image}. The error: {error}')

        if registry_data:
            digest = registry_data.attrs['Descriptor']['digest']
            updated_image = f'{image}@{digest}'

            if image_sha == digest:
                updated_image = False
                log.debug(f'{image}@{image_sha}: No update available')

        return updated_image

    def __get_image_parts(self, image_with_digest):
        image_parts = image_with_digest.split('@', 1)
        image = image_parts[0]
        image_sha = None

        # if there's no sha in the image name, restart the service **with** sha
        try:
            image_sha = image_parts[1]
        except IndexError:
            pass

        return image, image_sha

    def __update_image(self, service, update_image):
        service_name = service.name
        log.info(f'Updating service {service_name} with image {update_image}')
        service_updated = False
        try:
            service.update(image=update_image, force_update=True)
            service_updated = True
        except docker.errors.APIError as error:
            log.error(f'Failed to update {service_name}. The error: {error}')
        else:
            log.warning(f'Service {service_name} has been updated')
        return service_updated

    @prometheus.PROM_UPDATE_SUMMARY.time()
    def _run(self):
        """ the actual run """
        services = self.get_services()

        # prometheus metrics first
        for service in services:
            service_name = service.name
            try:
                prometheus.PROM_SVC_UPDATE_COUNTER.labels(service_name, service.id, service.short_id).inc(0)
            except docker.errors.NotFound:
                log.warning(f'Service {service_name} disappeared. Reloading the service list.')
                services = self.get_services()

        for service in services:
            service_name = service.name
            image_with_digest = service.attrs['Spec']['TaskTemplate']['ContainerSpec']['Image']
            image, image_sha = self.__get_image_parts(image_with_digest)
            update_image = self.__get_updated_image(image_sha=image_sha, image=image)
            service_updated = False
            if update_image:
                service_updated = self.__update_image(service, update_image)

            if service_updated:
                updating = True
                while updating:
                    try:
                        service.reload()
                        service_updated = True
                    except docker.errors.NotFound as error:
                        log.error(f'Exception caught: {error}')
                        log.warning('Service {service_name} disappeared. Reloading the service list.')
                        services = self.get_services()
                        service_updated = True
                        break

                    if service.attrs.get('UpdateStatus') and service.attrs['UpdateStatus'].get('State') == 'updating':
                        log.debug(f'Service {service_name} is in status `updating`. Waiting 1s...')
                        pause.seconds(1)
                    else:
                        log.debug(f'Service {service_name} has converged.')
                        updating = False

            if service_updated:
                prometheus.PROM_SVC_UPDATE_COUNTER.labels(service_name, service.id, service.short_id).inc(1)
                notify = {
                    'service_name': service_name,
                    'service_short_id': service.short_id,
                }
                if self.settings['notify_include_old_image']:
                    notify['old_image'] = image_with_digest
                if self.settings['notify_include_new_image']:
                    notify['new_image'] = service.attrs['Spec']['TaskTemplate']['ContainerSpec']['Image']
                self.notifiers.notify(**notify)

    def get_services(self):
        """ gets the list of services and filters out the black listed """
        services = self.docker.services.list(filters=self.settings['filters'])
        for blacklist_service in self.settings['blacklist']:
            for service in services:
                if service.name == blacklist_service:
                    log.debug(f'Blacklisted {blacklist_service}')
                    services.remove(service)
        return services