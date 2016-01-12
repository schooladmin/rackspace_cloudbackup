#!/usr/bin/env python
#Copyright 2013 Rackspace Hosting, Inc.

#Licensed under the Apache License, Version 2.0 (the "License");
#you may not use this file except in compliance with the License.
#You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
#Unless required by applicable law or agreed to in writing, software
#distributed under the License is distributed on an "AS IS" BASIS,
#WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#See the License for the specific language governing permissions and
#limitations under the License.


"""
exit codes:
  0 - success
  1 - generic failure
  2 - auth failure
  3 - backup api error
"""


import argparse
import json
import yaml
import urllib2
import sys
import syslog
import time

def cloud_auth(config, args):
    """
    Authenticate and return authentication details via returned dict
    """
    def cloud_auth_url_helper(val, target):
        if not val.has_key('region'):
            return False
        if val['region'] == target:
            return True
        return False

    if args.verbose:
        handler=urllib2.HTTPSHandler(debuglevel=10)
        opener = urllib2.build_opener(handler)
        urllib2.install_opener(opener)

    req = urllib2.Request("%s/v2.0/tokens" % args.identityurl,
                          headers = {'Content-type': 'application/json'}
                          )
    req.add_data(json.dumps({'auth': {'RAX-KSKEY:apiKeyCredentials':
                                          {'username': config["authentication"]["apiuser"],
                                           'apiKey': config["authentication"]["apikey"]}
                                      }
                             }))
    uh = urllib2.urlopen(req)
    if uh.getcode() is not 200:
        syslog.syslog("run_backup: INFO: Error requsting identity API data")
        sys.exit(2)
    json_response = json.loads(uh.read())

    # process the request
    if args.verbose:
        print 'JSON decoded and pretty'
        print json.dumps(json_response, indent=2)
    try:
        token = json_response['access']['token']['id']
        if args.verbose:
            print 'Token:\t\t', token

        backup_catalog = next((catalog for catalog in json_response['access']['serviceCatalog'] if catalog['name'] == 'cloudBackup'), None)
        if backup_catalog is None:
            syslog.syslog("run_backup: ERROR: Unable to locate cloudBackup in service catalog")
            sys.exit(2)

        backup_catalog_region = next((region for region in backup_catalog['endpoints'] if cloud_auth_url_helper(region, config['general']['region']) is True), None)
        if backup_catalog_region is None:
            syslog.syslog("run_backup: ERROR: Unable to locate cloudBackup endpoint in service catalog for region %s" % config['general']['region'])
            sys.exit(2)
        api_url = backup_catalog_region['publicURL']

    except(KeyError, IndexError):
        #print 'Error while getting answers from auth server.'
        #print 'Check the endpoint and auth credentials.'
        syslog.syslog("run_backup: Error parsing auth API response")
        sys.exit(2)
    finally:
        return {
            'token':   token,
            'api_url': api_url
            }

def triggerBackup(locationKey, config, tokenData, args):
    """
    Make a API request to start a backup.
    Returns True on success and False on failure
    """

    if config["locations"][locationKey]["enabled"] is False:
        if args.verbose:
            print "INFO: Skipping disabled location %s" % locationKey
        return True

    if args.verbose:
        handler=urllib2.HTTPSHandler(debuglevel=10)
        opener = urllib2.build_opener(handler)
        urllib2.install_opener(opener)

    # Format from http://docs.rackspace.com/rcbu/api/v1.0/rcbu-devguide/content/startBackup.html
    req = urllib2.Request("%s/backup/action-requested/" % tokenData['api_url'],
                          headers = {'Content-type': 'application/json',
                                     'X-Auth-Token': tokenData['token']}
                          )
    req.add_data(json.dumps({"Action": "StartManual",
                             "Id": config["locations"][locationKey]["backupConfigurationId"] }))

    uh = None
    status = None

    try:
        uh = urllib2.urlopen(req)
        status = uh.getcode()
    except urllib2.HTTPError, e:
        status = e.getcode()

    if uh is None or uh.getcode() is not 200:
        syslog.syslog("run_backup: INFO: Error triggering backup for %s with BackupConfigurationId %s, status code: %s" %
                      (locationKey, config["locations"][locationKey]["BackupConfigurationId"], status))
        return False

    syslog.syslog("run_backup: INFO: Triggered backup for %s, Backup job ID: %s" % (locationKey, uh.read()))
    return True

def awakenAgents(args, config, tokenData):
    """
    Make a API request to wake the agents
    """

    if args.verbose:
        handler=urllib2.HTTPSHandler(debuglevel=10)
        opener = urllib2.build_opener(handler)
        urllib2.install_opener(opener)

    # http://docs.rackspace.com/rcbu/api/v1.0/rcbu-devguide/content/Wake-Up_Agents-d1003.html
    req = urllib2.Request("%s/user/wakeupagents" % tokenData['api_url'],
                          headers = {'Content-type': 'application/json',
                                     'X-Auth-Token': tokenData['token']}
                          )
    req.add_data("") # A POST is required

    uh = None
    retries = 3

    for i in range(0, retries):
        try:
            uh = urllib2.urlopen(req)
            break
        except urllib2.HTTPError, e:
            syslog.syslog("run_backup: ERROR: Error waking up agents - %s - attempt %i" % (e, i + 1))
            if i < retries:
                time.sleep(7)

    if not uh or uh.getcode() is not 200:
        syslog.syslog("run_backup: ERROR: Error waking up agents - failed")
        sys.exit(3)

    # "You should wait 10-20 seconds after using this operation and then start a backup or restore."
    # http://docs.rackspace.com/rcbu/api/v1.0/rcbu-devguide/content/Wake-Up_Agents-d1003.html
    time.sleep(args.wakedelay)

def loadConfig(args):
    """
    Load json config file and return its contents
    """
    try:
        conf = yaml.load(open(args.conffile))
    except:
        syslog.syslog("run_backup: ERROR: Failed to read %s" % (args.conffile))
        sys.exit(1)

    if conf['general']['configRevision'] != 2:
        syslog.syslog("run_backup: ERROR: This version requires configuration revision 2" % (args.conffile))
        sys.exit(1)

    return conf

def parseArguments():
    """
    Parse command line arguments
    """
    parser = argparse.ArgumentParser(description='Triggers Rackspace Cloud Backup Jobs',
                                     formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--conffile', action='store', default="/etc/driveclient/run_backup.conf.yaml", help="YAML Configuration file to load", type=str)
    parser.add_argument('--wakedelay', action='store', default=30, help="Number of seconds to delay after waking agents (DELAY REQUIRED)", type=int)
    parser.add_argument('--identityurl', action='store', default="https://identity.api.rackspacecloud.com", help="Rackspace Identity API URL", type=str)
    parser.add_argument('--verbose', '-v', action='store_true', help='Turn up verbosity to 10')
    parser.add_argument('--location', action='store', default=None, help="Specific location to back up", type=str)

    args = parser.parse_args()

    return args


if __name__ == '__main__':
    args = parseArguments()
    config = loadConfig(args)

    if args.verbose:
        print "Config:"
        print config
        print ""

    if config["locations"] is None:
        syslog.syslog("run_backup: WARNING: No jobs configured!")
        sys.exit(1)

    tokenData = cloud_auth(config, args)
    awakenAgents(args, config, tokenData)

    if args.location is None:
        failure = False
        for location in config["locations"].keys():
            if not triggerBackup(location, config, tokenData, args):
                failure = True

        if failure:
            sys.exit(3)
    else:
        if not triggerBackup(args.location, config, tokenData, args):
            sys.exit(3)

    sys.exit(0)
