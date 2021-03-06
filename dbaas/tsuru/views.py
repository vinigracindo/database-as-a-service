# -*- coding: utf-8 -*-
import re
import logging
from slugify import slugify
from util import get_credentials_for
from util.decorators import REDIS_CLIENT
from util import simple_health_check
from logical.models import Database
from physical.models import Plan, Environment
from account.models import AccountUser, Team
from notification.models import TaskHistory
from notification.tasks import create_database
from dbaas_aclapi.tasks import bind_address_on_database
from dbaas_aclapi.tasks import unbind_address_on_database
from dbaas_aclapi.models import DatabaseBind
from dbaas_aclapi.models import DESTROYING, CREATED, CREATING
from dbaas_credentials.models import CredentialType
from django.core.exceptions import MultipleObjectsReturned
from django.db import transaction
from django.db import IntegrityError
from django.db.models import Q
from django.core.exceptions import ObjectDoesNotExist
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.renderers import JSONRenderer, JSONPRenderer
from rest_framework.response import Response
import requests
from networkapiclient import Ip, Network
from logical.validators import database_name_evironment_constraint
from system import models


LOG = logging.getLogger(__name__)


class ListPlans(APIView):
    renderer_classes = (JSONRenderer, JSONPRenderer)
    model = Plan

    def get(self, request, format=None):
        hard_plans = Plan.objects.filter(
            environments__name=get_url_env(request)
        ).values(
            'name', 'description', 'environments__name'
        ).extra(
            where=['is_active=True', 'provider={}'.format(Plan.CLOUDSTACK)]
        )
        return Response(get_plans_dict(hard_plans))


class GetServiceStatus(APIView):
    renderer_classes = (JSONRenderer, JSONPRenderer)
    model = Database

    def get(self, request, database_name, format=None):
        env = get_url_env(request)
        LOG.info("Database name {}. Environment {}".format(
            database_name, env)
        )
        try:
            database = get_database(database_name, env)
            database_status = database.status
        except IndexError as e:
            database_status = 0
            LOG.warn("There is not a database with this {} name on {}. {}".format(
                database_name, env, e)
            )

        LOG.info("Status = {}".format(database_status))
        task = TaskHistory.objects.filter(
            Q(arguments__contains=database_name) &
            Q(arguments__contains=env), task_status="RUNNING"
        ).order_by("created_at")

        LOG.info("Task {}".format(task))

        if database_status == Database.ALIVE:
            database_status = status.HTTP_204_NO_CONTENT
        elif database_status == Database.DEAD and not task:
            database_status = status.HTTP_500_INTERNAL_SERVER_ERROR
        else:
            database_status = status.HTTP_202_ACCEPTED

        return Response(status=database_status)


class GetServiceInfo(APIView):
    renderer_classes = (JSONRenderer, JSONPRenderer)
    model = Database

    def get(self, request, database_name, format=None):
        env = get_url_env(request)
        try:
            database = get_database(database_name, env)
            info = {'used_size_in_bytes': str(database.used_size_in_bytes)}
        except IndexError as e:
            info = {}
            LOG.warn(
                "There is not a database {} on {}. {}".format(
                    database_name, env, e
                )
            )

        LOG.info("Info = {}".format(info))

        return Response(info)


class ServiceAppBind(APIView):
    renderer_classes = (JSONRenderer, JSONPRenderer)
    model = Database

    def post(self, request, database_name, format=None):
        env = get_url_env(request)
        data = request.DATA
        LOG.debug("Request DATA {}".format(data))

        response = check_database_status(database_name, env)
        if type(response) != Database:
            return response

        database = response

        if database.databaseinfra.engine.name == 'redis':
            redis_password = database.databaseinfra.password
            endpoint = database.get_endpoint_dns().replace(
                '<password>', redis_password
            )

            env_vars = {
                "DBAAS_REDIS_PASSWORD": redis_password,
                "DBAAS_REDIS_ENDPOINT": endpoint
            }

            if database.plan.is_ha:
                env_vars = {
                    "DBAAS_SENTINEL_PASSWORD": redis_password,
                    "DBAAS_SENTINEL_ENDPOINT": endpoint,
                    "DBAAS_SENTINEL_ENDPOINT_SIMPLE": database.get_endpoint_dns_simple(),
                    "DBAAS_SENTINEL_SERVICE_NAME": database.databaseinfra.name
                }

        else:
            try:
                credential = database.credentials.all()[0]
            except IndexError as e:
                msg = "Database {} in env {} does not have credentials.".format(
                    database_name, env
                )
                return log_and_response(
                    msg=msg, e=e,
                    http_status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )

            endpoint = database.get_endpoint_dns().replace(
                '<user>:<password>', "{}:{}".format(
                    credential.user, credential.password
                )
            )

            kind = ''
            if endpoint.startswith('mysql'):
                kind = 'MYSQL_'
            if endpoint.startswith('mongodb'):
                kind = 'MONGODB_'

            env_vars = {
                "DBAAS_{}USER".format(kind): credential.user,
                "DBAAS_{}PASSWORD".format(kind): credential.password,
                "DBAAS_{}ENDPOINT".format(kind): endpoint
            }

        return Response(env_vars, status.HTTP_201_CREATED)

    def delete(self, request, database_name, format=None):
        env = get_url_env(request)
        data = request.DATA
        LOG.debug("Request DATA {}".format(data))

        response = check_database_status(database_name, env)
        if type(response) != Database:
            return response

        return Response(status.HTTP_204_NO_CONTENT)


class ServiceUnitBind(APIView):
    renderer_classes = (JSONRenderer, JSONPRenderer)
    model = Database

    def post(self, request, database_name, format=None):
        env = get_url_env(request)

        database = check_database_status(database_name, env)
        if type(database) != Database:
            return database

        data = request.DATA
        LOG.debug("Request DATA {}".format(data))

        unit_network = check_acl_service_and_get_unit_network(database, data)
        if type(unit_network) == Response:
            return unit_network

        created = False
        transaction.set_autocommit(False)
        database_bind = DatabaseBind(
            database=database, bind_address=unit_network, binds_requested=1
        )

        try:
            database_bind.save()
            created = True
        except IntegrityError as e:
            LOG.info("IntegrityError: {}".format(e))

            try:
                db_bind = DatabaseBind.objects.get(database=database,
                                                   bind_address=unit_network)

                bind = DatabaseBind.objects.select_for_update().filter(
                    id=db_bind.id)[0]
                if bind.bind_status in [CREATED, CREATING]:
                    bind.binds_requested += 1
                    bind.save()
                else:
                    raise Exception("Binds are beeing destroyed!")
            except Exception as e:
                LOG.debug("DatabaseBind is under destruction! {}".format(e))
                msg = "We are destroying your binds to {}. Please wait.".format(
                    database_name
                )
                return log_and_response(
                    msg=msg, e=e,
                    http_status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )

        finally:
            LOG.debug("Finishing transaction!")
            transaction.commit()
            transaction.set_autocommit(True)

        if created:
            bind_address_on_database.delay(
                database_bind=database_bind,
                user=request.user
            )

        return Response(None, status.HTTP_201_CREATED)

    def delete(self, request, database_name, format=None):
        env = get_url_env(request)
        data = request.DATA
        LOG.debug("Request DATA {}".format(data))

        response = check_database_status(database_name, env)
        if type(response) != Database:
            return response
        database = response

        unit_network = check_acl_service_and_get_unit_network(database, data)
        if type(unit_network) == Response:
            return unit_network

        transaction.set_autocommit(False)

        try:
            db_bind = DatabaseBind.objects.get(
                database=database, bind_address=unit_network
            )

            database_bind = DatabaseBind.objects.select_for_update().filter(
                id=db_bind.id
            )[0]

            if database_bind.bind_status == CREATING:
                raise Exception(
                    "Bind for {} has not yet been created!".format(
                        unit_network
                    )
                )

            if database_bind.bind_status != DESTROYING:
                if database_bind.binds_requested > 0:
                    database_bind.binds_requested -= 1

                if database_bind.binds_requested == 0:
                    database_bind.bind_status = DESTROYING

                database_bind.save()
        except (IndexError, ObjectDoesNotExist) as e:
            msg = "DatabaseBind does not exist"
            return log_and_response(
                msg=msg, e=e, http_status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
        except Exception as e:
            msg = "Bind for {} has not yet been created!".format(unit_network)
            return log_and_response(
                msg=msg, e=e, http_status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
        finally:
            LOG.debug("Finishing transaction!")
            transaction.commit()
            transaction.set_autocommit(True)

        if database_bind.binds_requested == 0:
            unbind_address_on_database.delay(
                database_bind=database_bind, user=request.user
            )

        return Response(status.HTTP_204_NO_CONTENT)


class ServiceAdd(APIView):

    renderer_classes = (JSONRenderer, JSONPRenderer)
    model = Database

    def post(self, request, format=None):
        data = request.DATA
        name = data['name']
        user = data['user']
        team = data['team']
        env = get_url_env(request)

        try:
            description = data['description']
            if not description:
                raise Exception("A description must be provided")
        except Exception as e:
            msg = "A description must be provided."
            return log_and_response(
                msg=msg, http_status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        name_regexp = re.compile('^[a-z][a-z0-9_]+$')
        if name_regexp.match(name) is None:
            msg = "Your database name must match /^[a-z][a-z0-9_]+$/ ."
            return log_and_response(
                msg=msg, http_status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        try:
            Database.objects.get(name=name, environment__name=env)
            msg = "There is already a database called {} in {}.".format(
                name, env
            )
            return log_and_response(
                msg=msg, http_status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        except ObjectDoesNotExist:
            pass

        if database_name_evironment_constraint(name, env):
            msg = "{} already exists in production!".format(name)
            return log_and_response(
                msg=msg, http_status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        try:
            dbaas_user = AccountUser.objects.get(email=user)
        except ObjectDoesNotExist as e:
            msg = "User does not exist."
            return log_and_response(
                msg=msg, e=e, http_status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        try:
            dbaas_team = Team.objects.get(name=team)
        except ObjectDoesNotExist as e:
            msg = "Team does not exist."
            return log_and_response(
                msg=msg, e=e, http_status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        try:
            dbaas_user.team_set.get(name=dbaas_team.name)
        except ObjectDoesNotExist as e:
            msg = "The user is not on {} team.".format(dbaas_team.name)
            return log_and_response(
                msg=msg, e=e, http_status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        try:
            dbaas_environment = Environment.objects.get(name=env)
        except(ObjectDoesNotExist) as e:
            msg = "Environment does not exist."
            return log_and_response(
                msg=msg, http_status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        databases_used_by_team = dbaas_team.count_databases_in_use(
            environment=dbaas_environment
        )
        database_alocation_limit = dbaas_team.database_alocation_limit

        if databases_used_by_team >= database_alocation_limit:
            msg = "The database alocation limit of {} has been exceeded for the selected team: {}".format(
                database_alocation_limit, dbaas_team
            )
            return log_and_response(
                msg=msg, http_status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        if 'plan' not in data:
            msg = "Plan was not found"
            return log_and_response(
                msg=msg, http_status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        plan = data['plan']
        hard_plans = Plan.objects.values(
            'name', 'description', 'pk', 'environments__name'
        ).extra(
            where=['is_active=True', 'provider={}'.format(Plan.CLOUDSTACK)]
        )

        plans = get_plans_dict(hard_plans)
        plan = [splan for splan in plans if splan['name'] == plan]
        LOG.info("Plan: {}".format(plan))

        if any(plan):
            dbaas_plan = Plan.objects.get(pk=plan[0]['pk'])
        else:
            msg = "Plan was not found"
            return log_and_response(
                msg=msg, http_status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        task_history = TaskHistory()
        task_history.task_name = "create_database"
        task_history.arguments = "Database name: {}".format(name)
        task_history.save()

        create_database.delay(
            name=name, plan=dbaas_plan, environment=dbaas_environment,
            team=dbaas_team, project=None, description=description,
            contacts=None,
            task_history=task_history, user=dbaas_user
        )

        return Response(status=status.HTTP_201_CREATED)


class ServiceRemove(APIView):
    renderer_classes = (JSONRenderer, JSONPRenderer)
    model = Database

    def delete(self, request, database_name, format=None):
        env = get_url_env(request)
        try:
            database = get_database(database_name, env)
        except IndexError as e:
            msg = "Database id provided does not exist {} in {}.".format(
                database_name, env)
            return log_and_response(
                msg=msg, e=e, http_status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        database.delete()
        return Response(status.HTTP_204_NO_CONTENT)


def get_plans_dict(hard_plans):
    plans = []
    for hard_plan in hard_plans:
        hard_plan['description'] = hard_plan[
            'name'] + '-' + hard_plan['environments__name']
        hard_plan['name'] = slugify(hard_plan['description'])
        del hard_plan['environments__name']
        plans.append(hard_plan)

    return plans


def get_url_env(request):
    env = request._request.path.split('/')[1]
    if env == 'prod':
        env = 'prod-cm'
    return env


def log_and_response(msg, http_status, e="Conditional Error."):
    LOG.warn(msg)
    LOG.warn("Error: {}".format(e))
    return Response(msg, http_status)


def check_database_status(database_name, env):
    task = TaskHistory.objects.filter(
        arguments__contains="Database name: {}, Environment: {}".format(
            database_name, env
        ), task_status="RUNNING")

    LOG.info("Task {}".format(task))
    if task:
        msg = "Database {} in env {} is beeing created.".format(
            database_name, env)
        return log_and_response(
            msg=msg, http_status=status.HTTP_412_PRECONDITION_FAILED)

    try:
        database = get_database(database_name, env)
    except IndexError as e:
        msg = "Database {} does not exist in env {}.".format(
            database_name, env)
        return log_and_response(
            msg=msg, e=e, http_status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    except MultipleObjectsReturned as e:
        msg = "There are multiple databases called {} in {}.".format(
            database_name, env)
        return log_and_response(
            msg=msg, e=e, http_status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    except Exception as e:
        msg = "Something ocurred on dbaas, please get in touch with your DBA."
        return log_and_response(
            msg=msg, e=e, http_status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    if not(database and database.status):
        msg = "Database {} is not Alive.".format(database_name)
        return log_and_response(
            msg=msg, http_status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    return database


def get_network_from_ip(ip, database_environment):
    net_api_credentials = get_credentials_for(
        environment=database_environment,
        credential_type=CredentialType.NETWORKAPI
    )

    ip_client = Ip.Ip(
        net_api_credentials.endpoint, net_api_credentials.user,
        net_api_credentials.password
    )

    ips = ip_client.get_ipv4_or_ipv6(ip)
    ips = ips['ips']
    if type(ips) != list:
        ips = [ips]

    net_ip = ips[0]
    network_client = Network.Network(
        net_api_credentials.endpoint, net_api_credentials.user,
        net_api_credentials.password
    )

    network = network_client.get_network_ipv4(net_ip['networkipv4'])
    network = network['network']

    return network['oct1'] + '.' + network['oct2'] + '.' + network['oct3'] + '.' + network['oct4'] + '/' + network['block']


def get_database(name, env):
    if env in models.Configuration.get_by_name_as_list('dev_envs'):
        database = Database.objects.filter(
            name=name, environment__name=env
        ).exclude(is_in_quarantine=True)[0]
    else:
        prod_envs = models.Configuration.get_by_name_as_list('prod_envs')
        database = Database.objects.filter(
            name=name, environment__name__in=prod_envs
        ).exclude(is_in_quarantine=True)[0]

    return database


def check_acl_service_and_get_unit_network(database, data):
    acl_credential = get_credentials_for(
        environment=database.environment,
        credential_type=CredentialType.ACLAPI
    )
    health_check_info = acl_credential.get_parameters_by_group('hc')
    try:
        health_check_url = acl_credential.endpoint + health_check_info['health_check_url']
        simple_hc = simple_health_check.SimpleHealthCheck(
            health_check_url=health_check_url,
            service_key=health_check_info['key_name'],
            redis_client=REDIS_CLIENT, http_client=requests,
            http_request_exceptions=(Exception,), verify_ssl=False,
            health_check_request_timeout=int(health_check_info['timeout'])
        )
    except KeyError as e:
        msg = "AclApi Credential configured improperly."
        return log_and_response(
            msg=msg, e=e,
            http_status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )

    try:
        simple_hc.check_service()
    except simple_health_check.HealthCheckError as e:
        LOG.warn(e)
        msg = "We are experiencing errors with the acl api, please try again later."
        return log_and_response(
            msg=msg, e=e,
            http_status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )
    except Exception as e:
        LOG.warn(e)

    try:
        return get_network_from_ip(
            data.get('unit-host'), database.environment
        )
    except Exception as e:
        LOG.warn(e)
        msg = "We are experiencing errors with the network api, please try again later"
        return log_and_response(
            msg=msg, e=e,
            http_status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )
