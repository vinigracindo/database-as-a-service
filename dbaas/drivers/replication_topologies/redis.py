# -*- coding: utf-8 -*-
from base import BaseTopology, STOP_RESIZE_START


class BaseRedis(BaseTopology):

    def get_deploy_steps(self):
        return (
            'workflow.steps.redis.deploy.build_databaseinfra.BuildDatabaseInfra',
            'workflow.steps.redis.deploy.create_virtualmachines.CreateVirtualMachine',
            'workflow.steps.redis.deploy.create_dns.CreateDns',
            'workflow.steps.util.deploy.create_nfs.CreateNfs',
            'workflow.steps.redis.deploy.init_database.InitDatabaseRedis',
            'workflow.steps.util.deploy.config_backup_log.ConfigBackupLog',
            'workflow.steps.util.deploy.check_database_connection.CheckDatabaseConnection',
            'workflow.steps.util.deploy.check_dns.CheckDns',
            'workflow.steps.util.deploy.create_zabbix.CreateZabbix',
            'workflow.steps.util.deploy.start_monit.StartMonit',
            'workflow.steps.util.deploy.create_dbmonitor.CreateDbMonitor',
            'workflow.steps.util.deploy.build_database.BuildDatabase',
            'workflow.steps.util.deploy.create_log.CreateLog',
            'workflow.steps.util.deploy.check_database_binds.CheckDatabaseBinds',
        )

    def get_clone_steps(self):
        return self.get_deploy_steps() + (
            'workflow.steps.redis.clone.clone_database.CloneDatabase',
            'workflow.steps.util.resize.check_database_status.CheckDatabaseStatus',
        )

    def get_resize_steps(self):
        return (
            ('workflow.steps.util.volume_migration.stop_database.StopDatabase',
             'workflow.steps.redis.resize.change_config.ChangeDatabaseConfigFile',) +
            STOP_RESIZE_START +
            ('workflow.steps.util.resize.start_database.StartDatabase',
             'workflow.steps.util.resize.start_agents.StartAgents',
             'workflow.steps.util.resize.check_database_status.CheckDatabaseStatus',)
        )


class RedisSingle(BaseRedis):
    pass


class RedisSentinel(BaseRedis):
    pass
