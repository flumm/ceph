"""
Client module to interact with the Ansible Runner Service
"""

import json
import re
from functools import wraps

import requests

# Ansible Runner service API endpoints
API_URL = "api"
PLAYBOOK_EXEC_URL = "api/v1/playbooks"
PLAYBOOK_EVENTS = "api/v1/jobs/%s/events"
EVENT_DATA_URL = "api/v1/jobs/%s/events/%s"

class AnsibleRunnerServiceError(Exception):
    """Generic Ansible Runner Service Exception"""
    pass

def handle_requests_exceptions(func):
    """Decorator to manage errors raised by requests library
    """
    @wraps(func)
    def inner(*args, **kwargs):
        """Generic error mgmt decorator"""
        try:
            return func(*args, **kwargs)
        except (requests.exceptions.RequestException, IOError) as ex:
            raise AnsibleRunnerServiceError(str(ex))
    return inner

class ExecutionStatusCode(object):
    """Execution status of playbooks ( 'msg' field in playbook status request)
    """

    SUCCESS = 0   # Playbook has been executed succesfully" msg = successful
    ERROR = 1     # Playbook has finished with error        msg = failed
    ON_GOING = 2  # Playbook is being executed              msg = running
    NOT_LAUNCHED = 3  # Not initialized

class PlayBookExecution(object):
    """Object to provide all the results of a Playbook execution
    """

    def __init__(self, rest_client, playbook, logger, result_pattern="",
                 the_params=None,
                 querystr_dict=None):

        self.rest_client = rest_client

        # Identifier of the playbook execution
        self.play_uuid = "-"

        # Pattern used to extract the result from the events
        self.result_task_pattern = result_pattern

        # Playbook name
        self.playbook = playbook

        # Parameters used in the playbook
        self.params = the_params

        # Query string used in the "launch" request
        self.querystr_dict = querystr_dict

        # Logger
        self.log = logger

    def launch(self):
        """ Launch the playbook execution
        """

        response = None
        endpoint = "%s/%s" % (PLAYBOOK_EXEC_URL, self.playbook)

        try:
            response = self.rest_client.http_post(endpoint,
                                                  self.params,
                                                  self.querystr_dict)
        except AnsibleRunnerServiceError:
            self.log.exception("Error launching playbook <%s>", self.playbook)
            raise

        # Here we have a server response, but an error trying
        # to launch the playbook is also posible (ex. 404, playbook not found)
        # Error already logged by rest_client, but an error should be raised
        # to the orchestrator (via completion object)
        if response.ok:
            self.play_uuid = json.loads(response.text)["data"]["play_uuid"]
            self.log.info("Playbook execution launched succesfuly")
        else:
            raise AnsibleRunnerServiceError(response.reason)

    def get_status(self):
        """ Return the status of the execution

        In the msg field of the respons we can find:
            "msg": "successful"
            "msg": "running"
            "msg": "failed"
        """

        status_value = ExecutionStatusCode.NOT_LAUNCHED
        response = None

        if self.play_uuid == '-': # Initialized
            status_value = ExecutionStatusCode.NOT_LAUNCHED
        elif self.play_uuid == '': # Error launching playbook
            status_value = ExecutionStatusCode.ERROR
        else:
            endpoint = "%s/%s" % (PLAYBOOK_EXEC_URL, self.play_uuid)

            try:
                response = self.rest_client.http_get(endpoint)
            except AnsibleRunnerServiceError:
                self.log.exception("Error getting playbook <%s> status",
                                   self.playbook)

            if response:
                the_status = json.loads(response.text)["msg"]
                if the_status == 'successful':
                    status_value = ExecutionStatusCode.SUCCESS
                elif the_status == 'failed':
                    status_value = ExecutionStatusCode.ERROR
                else:
                    status_value = ExecutionStatusCode.ON_GOING
            else:
                status_value = ExecutionStatusCode.ERROR

        self.log.info("Requested playbook execution status is: %s", status_value)
        return status_value

    def get_result(self, event_filter):
        """Get the data of the events filtered by a task pattern and
        a event filter

        @event_filter: list of 0..N event names items
        @returns: the events that matches with the patterns provided
        """
        response = None
        if not self.play_uuid:
            return {}

        try:
            response = self.rest_client.http_get(PLAYBOOK_EVENTS % self.play_uuid)
        except AnsibleRunnerServiceError:
            self.log.exception("Error getting playbook <%s> result", self.playbook)

        if not response:
            result_events = {}
        else:
            events = json.loads(response.text)["data"]["events"]

            # Filter by task
            if self.result_task_pattern:
                result_events = {event:data for event, data in events.items()
                                 if "task" in data and
                                 re.match(self.result_task_pattern, data["task"])}
            else:
                result_events = events

            # Filter by event
            if event_filter:
                type_of_events = "|".join(event_filter)

                result_events = {event:data for event, data in result_events.items()
                                 if re.match(type_of_events, data['event'])}

        self.log.info("Requested playbook result is: %s", json.dumps(result_events))
        return result_events

class Client(object):
    """An utility object that allows to connect with the Ansible runner service
    and execute easily playbooks
    """

    def __init__(self, server_url, verify_server, ca_bundle, client_cert,
                 client_key, logger):
        """Provide an https client to make easy interact with the Ansible
        Runner Service"

        :param server_url: The base URL >server>:<port> of the Ansible Runner
                           Service
        :param verify_server: A boolean to specify if server authentity should
                              be checked or not. (True by default)
        :param ca_bundle: If provided, an alternative Cert. Auth. bundle file
                          will be used as source for checking the authentity of
                          the Ansible Runner Service
        :param client_cert: Path to Ansible Runner Service client certificate
                            file
        :param client_key: Path to Ansible Runner Service client certificate key
                           file
        :param logger: Log file
        """
        self.server_url = server_url
        self.log = logger
        self.client_cert = (client_cert, client_key)

        # used to provide the "verify" parameter in requests
        # a boolean that sometimes contains a string :-(
        self.verify_server = verify_server
        if ca_bundle: # This intentionallly overwrites
            self.verify_server = ca_bundle

        self.server_url = "https://{0}".format(self.server_url)

    @handle_requests_exceptions
    def is_operative(self):
        """Indicates if the connection with the Ansible runner Server is ok
        """

        # Check the service
        response = self.http_get(API_URL)

        if response:
            return response.status_code == requests.codes.ok
        else:
            return False

    @handle_requests_exceptions
    def http_get(self, endpoint):
        """Execute an http get request

        :param endpoint: Ansible Runner service RESTful API endpoint

        :returns: A requests object
        """

        the_url = "%s/%s" % (self.server_url, endpoint)

        response = requests.get(the_url,
                                verify=self.verify_server,
                                cert=self.client_cert,
                                headers={})

        if response.status_code != requests.codes.ok:
            self.log.error("http GET %s <--> (%s - %s)\n%s",
                           the_url, response.status_code, response.reason,
                           response.text)
        else:
            self.log.info("http GET %s <--> (%s - %s)",
                          the_url, response.status_code, response.text)

        return response

    @handle_requests_exceptions
    def http_post(self, endpoint, payload, params_dict):
        """Execute an http post request

        :param endpoint: Ansible Runner service RESTful API endpoint
        :param payload: Dictionary with the data used in the post request
        :param params_dict: A dict used to build a query string

        :returns: A requests object
        """

        the_url = "%s/%s" % (self.server_url, endpoint)
        response = requests.post(the_url,
                                 verify=self.verify_server,
                                 cert=self.client_cert,
                                 headers={"Content-type": "application/json"},
                                 json=payload,
                                 params=params_dict)

        if response.status_code != requests.codes.ok:
            self.log.error("http POST %s [%s] <--> (%s - %s:%s)\n",
                           the_url, payload, response.status_code,
                           response.reason, response.text)
        else:
            self.log.info("http POST %s <--> (%s - %s)",
                          the_url, response.status_code, response.text)

        return response

    @handle_requests_exceptions
    def http_delete(self, endpoint):
        """Execute an http delete request

        :param endpoint: Ansible Runner service RESTful API endpoint

        :returns: A requests object
        """

        the_url = "%s/%s" % (self.server_url, endpoint)
        response = requests.delete(the_url,
                                   verify=self.verify_server,
                                   cert=self.client_cert,
                                   headers={})

        if response.status_code != requests.codes.ok:
            self.log.error("http DELETE %s <--> (%s - %s)\n%s",
                           the_url, response.status_code, response.reason,
                           response.text)
        else:
            self.log.info("http DELETE %s <--> (%s - %s)",
                          the_url, response.status_code, response.text)

        return response

    def http_put(self, endpoint, payload):
        """Execute an http put request

        :param endpoint: Ansible Runner service RESTful API endpoint
        :param payload: Dictionary with the data used in the put request

        :returns: A requests object
        """
        # TODO
        raise NotImplementedError("TODO")
