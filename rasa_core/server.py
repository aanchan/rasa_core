import logging
import os
import tempfile
import zipfile
import datetime
import re
from functools import wraps, reduce
from inspect import isawaitable
from typing import Any, Callable, List, Optional, Text, Union

from sanic import Sanic, response
from sanic.exceptions import NotFound
from sanic.request import Request
from sanic_cors import CORS
from sanic_jwt import Initialize, exceptions

import rasa
from rasa_core import constants, utils
from rasa_core.channels import CollectingOutputChannel, UserMessage, OutputChannel
from rasa_core.domain import Domain
from rasa_core.events import Event
from rasa_core.policies import PolicyEnsemble
from rasa_core.test import test
from rasa_core.trackers import EventVerbosity, DialogueStateTracker
from rasa_core.utils import dump_obj_as_str_to_file
from rasa_core.agent import load_agent
from rasa_core.utils import EndpointConfig
from rasa_core.utils import AvailableEndpoints
from rasa_core.agent import Agent
from rasa_core.interpreter import NaturalLanguageInterpreter
from rasa_core.tracker_store import TrackerStore


logger = logging.getLogger(__name__)

OUTPUT_CHANNEL_QUERY_KEY = "output_channel"
USE_LATEST_INPUT_CHANNEL_AS_OUTPUT_CHANNEL = "latest"

class ErrorResponse(Exception):
    def __init__(self, status, reason, message, details=None, help_url=None):
        self.error_info = {
            "version": rasa.__version__,
            "status": "failure",
            "message": message,
            "reason": reason,
            "details": details or {},
            "help": help_url,
            "code": status
        }
        self.status = status


def _docs(sub_url: Text) -> Text:
    """Create a url to a subpart of the docs."""
    return constants.DOCS_BASE_URL + sub_url


def ensure_loaded_agent(app):
    """Wraps a request handler ensuring there is a loaded and usable model."""

    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if not app.agent or not app.agent.is_ready():
                raise ErrorResponse(
                    503,
                    "NoAgent",
                    "No agent loaded. To continue processing, a "
                    "model of a trained agent needs to be loaded.",
                    help_url=_docs("/server.html#running-the-http-server"))

            return f(*args, **kwargs)

        return decorated

    return decorator


def request_parameters(request):
    if request.method == 'GET':
        return request.raw_args
    else:
        try:
            return request.json
        except ValueError as e:
            logger.error("Failed to decode json during respond request. "
                         "Error: {}.".format(e))
            raise


def requires_auth(app: Sanic,
                  token: Optional[Text] = None
                  ) -> Callable[[Any], Any]:
    """Wraps a request handler with token authentication."""

    def decorator(f: Callable[[Any, Any, Any], Any]
                  ) -> Callable[[Any, Any], Any]:
        def sender_id_from_args(args: Any,
                                kwargs: Any) -> Optional[Text]:
            argnames = utils.arguments_of(f)
            try:
                sender_id_arg_idx = argnames.index("sender_id")
                if "sender_id" in kwargs:  # try to fetch from kwargs first
                    return kwargs["sender_id"]
                if sender_id_arg_idx < len(args):
                    return args[sender_id_arg_idx]
                return None
            except ValueError:
                return None

        def sufficient_scope(request,
                             *args: Any,
                             **kwargs: Any) -> Optional[bool]:
            jwt_data = request.app.auth.extract_payload(request)
            user = jwt_data.get("user", {})

            username = user.get("username", None)
            role = user.get("role", None)

            if role == "admin":
                return True
            elif role == "user":
                sender_id = sender_id_from_args(args, kwargs)
                return sender_id is not None and username == sender_id
            else:
                return False

        @wraps(f)
        async def decorated(request: Request,
                            *args: Any,
                            **kwargs: Any) -> Any:

            provided = utils.default_arg(request, 'token', None)
            # noinspection PyProtectedMember
            if token is not None and provided == token:
                result = f(request, *args, **kwargs)
                if isawaitable(result):
                    result = await result
                return result
            elif (app.config.get('USE_JWT') and
                  request.app.auth.is_authenticated(request)):
                if sufficient_scope(request, *args, **kwargs):
                    result = f(request, *args, **kwargs)
                    if isawaitable(result):
                        result = await result
                    return result
                raise ErrorResponse(
                    403, "NotAuthorized",
                    "User has insufficient permissions.",
                    help_url=_docs(
                        "/server.html#security-considerations"))
            elif token is None and app.config.get('USE_JWT') is None:
                # authentication is disabled
                result = f(request, *args, **kwargs)
                if isawaitable(result):
                    result = await result
                return result
            raise ErrorResponse(
                401, "NotAuthenticated", "User is not authenticated.",
                help_url=_docs("/server.html#security-considerations"))

        return decorated

    return decorator


def event_verbosity_parameter(request, default_verbosity):
    event_verbosity_str = request.raw_args.get(
        'include_events', default_verbosity.name).upper()
    try:
        return EventVerbosity[event_verbosity_str]
    except KeyError:
        enum_values = ", ".join([e.name for e in EventVerbosity])
        raise ErrorResponse(404, "InvalidParameter",
                            "Invalid parameter value for 'include_events'. "
                            "Should be one of {}".format(enum_values),
                            {"parameter": "include_events", "in": "query"})


# noinspection PyUnusedLocal
async def authenticate(request):
    raise exceptions.AuthenticationFailed(
        "Direct JWT authentication not supported. You should already have "
        "a valid JWT from an authentication provider, Rasa will just make "
        "sure that the token is valid, but not issue new tokens.")


def configure_cors(app: Sanic, cors_origins: Union[Text, List[Text]] = "") -> None:
    """Configure CORS origins for the given app."""

    # Workaround so that socketio works with requests from other origins.
    # https://github.com/miguelgrinberg/python-socketio/issues/205#issuecomment-493769183
    app.config.CORS_AUTOMATIC_OPTIONS = True
    app.config.CORS_SUPPORTS_CREDENTIALS = True

    CORS(app, resources={r"/*": {"origins": cors_origins}}, automatic_options=True)


def add_root_route(app: Sanic):
    @app.get("/")
    async def hello(request: Request):
        """Check if the server is running and responds with the version."""
        return response.text("Hello from Rasa: " + rasa.__version__)


def create_app(agent=None,
               cors_origins: Union[Text, List[Text]] = "*",
               auth_token: Optional[Text] = None,
               jwt_secret: Optional[Text] = None,
               jwt_method: Text = "HS256",
               endpoints: Optional[AvailableEndpoints] = None,
               ):
    """Class representing a Rasa Core HTTP server."""

    app = Sanic(__name__)
    app.config.RESPONSE_TIMEOUT = 60 * 60

    CORS(app,
         resources={r"/*": {"origins": cors_origins or ""}},
         automatic_options=True)

    # Setup the Sanic-JWT extension
    if jwt_secret and jwt_method:
        # since we only want to check signatures, we don't actually care
        # about the JWT method and set the passed secret as either symmetric
        # or asymmetric key. jwt lib will choose the right one based on method
        app.config['USE_JWT'] = True
        Initialize(app,
                   secret=jwt_secret,
                   authenticate=authenticate,
                   algorithm=jwt_method,
                   user_id="username")

    app.agent = agent

    @app.listener('after_server_start')
    async def warn_if_agent_is_unavailable(app, loop):
        if not app.agent or not app.agent.is_ready():
            logger.warning("The loaded agent is not ready to be used yet "
                           "(e.g. only the NLU interpreter is configured, "
                           "but no Core model is loaded). This is NOT AN ISSUE "
                           "some endpoints are not available until the agent "
                           "is ready though.")

    @app.exception(NotFound)
    @app.exception(ErrorResponse)
    async def ignore_404s(request: Request, exception: ErrorResponse):
        return response.json(exception.error_info,
                             status=exception.status)

    @app.get("/")
    async def hello(request: Request):
        """Check if the server is running and responds with the version."""
        return response.text("hello from Rasa: " + rasa.__version__)

    @app.get("/version")
    async def version(request: Request):
        """respond with the version number of the installed rasa core."""

        return response.json({
            "version": rasa.__version__,
            "minimum_compatible_version": constants.MINIMUM_COMPATIBLE_VERSION
        })

    # <sender_id> can be be 'default' if there's only 1 client
    @app.post("/conversations/<sender_id>/execute")
    @requires_auth(app, auth_token)
    @ensure_loaded_agent(app)
    async def execute_action(request: Request, sender_id: Text):
        request_params = request.json

        # we'll accept both parameters to specify the actions name
        action_to_execute = (request_params.get("name") or
                             request_params.get("action"))

        policy = request_params.get("policy", None)
        confidence = request_params.get("confidence", None)
        verbosity = event_verbosity_parameter(request,
                                              EventVerbosity.AFTER_RESTART)

        try:
            tracker = app.agent.tracker_store.get_or_create_tracker(sender_id)
            output_channel = _get_output_channel(request, tracker)
            logger.info('output_channel: {}'.format(output_channel))
            await app.agent.execute_action(sender_id,
                                           action_to_execute,
                                           output_channel,
                                           policy,
                                           confidence)

            # retrieve tracker and set to requested state
            tracker = app.agent.tracker_store.get_or_create_tracker(sender_id)
            state = tracker.current_state(verbosity)
            return response.json({"tracker": state})

        except ValueError as e:
            raise ErrorResponse(400, "ValueError", e)
        except Exception as e:
            logger.error("Encountered an exception while running action '{}'. "
                         "Bot will continue, but the actions events are lost. "
                         "Make sure to fix the exception in your custom "
                         "code.".format(action_to_execute))
            logger.debug(e, exc_info=True)
            raise ErrorResponse(500, "ValueError",
                                "Server failure. Error: {}".format(e))

    @app.post("/conversations/<sender_id>/tracker/events")
    @requires_auth(app, auth_token)
    @ensure_loaded_agent(app)
    async def append_event(request: Request, sender_id: Text):
        """Append a list of events to the state of a conversation"""

        request_params = request.json
        evt = Event.from_parameters(request_params)
        tracker = app.agent.tracker_store.get_or_create_tracker(sender_id)
        verbosity = event_verbosity_parameter(request,
                                              EventVerbosity.AFTER_RESTART)

        if evt:
            tracker.update(evt)
            app.agent.tracker_store.save(tracker)
            return response.json(tracker.current_state(verbosity))
        else:
            logger.warning(
                "Append event called, but could not extract a "
                "valid event. Request JSON: {}".format(request_params))
            raise ErrorResponse(400, "InvalidParameter",
                                "Couldn't extract a proper event from the "
                                "request body.",
                                {"parameter": "", "in": "body"})

    @app.put("/conversations/<sender_id>/tracker/events")
    @requires_auth(app, auth_token)
    @ensure_loaded_agent(app)
    async def replace_events(request: Request, sender_id: Text):
        """Use a list of events to set a conversations tracker to a state."""

        request_params = request.json
        verbosity = event_verbosity_parameter(request,
                                              EventVerbosity.AFTER_RESTART)

        tracker = DialogueStateTracker.from_dict(sender_id,
                                                 request_params,
                                                 app.agent.domain.slots)
        # will override an existing tracker with the same id!
        app.agent.tracker_store.save(tracker)
        return response.json(tracker.current_state(verbosity))

    @app.get("/conversations")
    @requires_auth(app, auth_token)
    async def list_trackers(request: Request):
        if app.agent.tracker_store:
            keys = list(app.agent.tracker_store.keys())
        else:
            keys = []

        return response.json(keys)

    @app.get("/conversations/<sender_id>/tracker")
    @requires_auth(app, auth_token)
    async def retrieve_tracker(request: Request, sender_id: Text):
        """Get a dump of a conversation's tracker including its events."""

        if not app.agent.tracker_store:
            raise ErrorResponse(503, "NoTrackerStore",
                                "No tracker store available. Make sure to "
                                "configure a tracker store when starting "
                                "the server.")

        # parameters
        default_verbosity = EventVerbosity.AFTER_RESTART

        # this is for backwards compatibility
        if "ignore_restarts" in request.raw_args:
            ignore_restarts = utils.bool_arg(request, 'ignore_restarts',
                                             default=False)
            if ignore_restarts:
                default_verbosity = EventVerbosity.ALL

        if "events" in request.raw_args:
            include_events = utils.bool_arg(request, 'events',
                                            default=True)
            if not include_events:
                default_verbosity = EventVerbosity.NONE

        verbosity = event_verbosity_parameter(request,
                                              default_verbosity)

        # retrieve tracker and set to requested state
        tracker = app.agent.tracker_store.get_or_create_tracker(sender_id)
        if not tracker:
            raise ErrorResponse(503,
                                "NoDomain",
                                "Could not retrieve tracker. Most likely "
                                "because there is no domain set on the agent.")

        until_time = utils.float_arg(request, 'until')
        if until_time is not None:
            tracker = tracker.travel_back_in_time(until_time)

        # dump and return tracker

        state = tracker.current_state(verbosity)
        return response.json(state)

    @app.get("/conversations/<sender_id>/story")
    @requires_auth(app, auth_token)
    async def retrieve_story(request: Request, sender_id: Text):
        """Get an end-to-end story corresponding to this conversation."""

        if not app.agent.tracker_store:
            raise ErrorResponse(503, "NoTrackerStore",
                                "No tracker store available. Make sure to "
                                "configure "
                                "a tracker store when starting the server.")

        # retrieve tracker and set to requested state
        tracker = app.agent.tracker_store.get_or_create_tracker(sender_id)
        if not tracker:
            raise ErrorResponse(503,
                                "NoDomain",
                                "Could not retrieve tracker. Most likely "
                                "because there is no domain set on the agent.")

        until_time = utils.float_arg(request, 'until')
        if until_time is not None:
            tracker = tracker.travel_back_in_time(until_time)

        # dump and return tracker
        state = tracker.export_stories(e2e=True)
        return response.text(state)

    @app.route("/conversations/<sender_id>/respond", methods=['GET', 'POST'])
    @requires_auth(app, auth_token)
    @ensure_loaded_agent(app)
    async def respond(request: Request, sender_id: Text):
        request_params = request_parameters(request)

        if 'query' in request_params:
            message = request_params['query']
        elif 'q' in request_params:
            message = request_params['q']
        else:
            raise ErrorResponse(400,
                                "InvalidParameter",
                                "Missing the message parameter.",
                                {"parameter": "query", "in": "query"})

        try:
            # Set the output channel
            out = CollectingOutputChannel()
            # Fetches the appropriate bot response in a json format
            responses = await app.agent.handle_text(message,
                                                    output_channel=out,
                                                    sender_id=sender_id)
            return response.json(responses)

        except Exception as e:
            logger.exception("Caught an exception during respond.")
            raise ErrorResponse(500, "ActionException",
                                "Server failure. Error: {}".format(e))

    @app.post("/conversations/<sender_id>/predict")
    @requires_auth(app, auth_token)
    @ensure_loaded_agent(app)
    async def predict(request: Request, sender_id: Text):
        try:
            # Fetches the appropriate bot response in a json format
            responses = app.agent.predict_next(sender_id)
            responses['scores'] = sorted(responses['scores'],
                                         key=lambda k: (-k['score'],
                                                        k['action']))
            return response.json(responses)

        except Exception as e:
            logger.exception("Caught an exception during prediction.")
            raise ErrorResponse(500, "PredictionException",
                                "Server failure. Error: {}".format(e))

    @app.post("/conversations/<sender_id>/messages")
    @requires_auth(app, auth_token)
    @ensure_loaded_agent(app)
    async def log_message(request: Request, sender_id: Text):
        request_params = request.json
        try:
            message = request_params["message"]
        except KeyError:
            message = request_params.get("text")

        sender = request_params.get("sender")
        parse_data = request_params.get("parse_data")
        verbosity = event_verbosity_parameter(request,
                                              EventVerbosity.AFTER_RESTART)

        # TODO: implement properly for agent / bot
        if sender != "user":
            raise ErrorResponse(500,
                                "NotSupported",
                                "Currently, only user messages can be passed "
                                "to this endpoint. Messages of sender '{}' "
                                "cannot be handled.".format(sender),
                                {"parameter": "sender", "in": "body"})

        try:
            usermsg = UserMessage(message, None, sender_id, parse_data)
            tracker = await app.agent.log_message(usermsg)
            return response.json(tracker.current_state(verbosity))

        except Exception as e:
            logger.exception("Caught an exception while logging message.")
            raise ErrorResponse(500, "MessageException",
                                "Server failure. Error: {}".format(e))

    @app.route("/conversations/<sender_id>", methods=["DELETE"])
    @requires_auth(app, auth_token)
    async def delete_conversation(request: Request, sender_id: Text):
        """ Delete conversation from an user within the tracker_store.

        Arguments:
            sender_id {Text} -- The user identification to delete conversation from

        Returns:
            responses -- Jsonify confirmation that user has been deleted.
        """

        if not app.agent.tracker_store:
            raise ErrorResponse(503, "NoTrackerStore",
                                "No tracker store available. Make sure to "
                                "configure a tracker store when starting "
                                "the server.")

        # retrieve tracker and set to requested state
        tracker = app.agent.tracker_store.get_or_create_tracker(sender_id)
        if not tracker:
            raise ErrorResponse(503,
                                "NoDomain",
                                "Could not retrieve tracker. Most likely "
                                "because there is no domain set on the agent.")

        # delete id from tracker
        tracker.delete(sender_id)

        # send confirmation of deletion as a response
        return response.json({sender_id: "User was successfully deleted from tracker."})

    @app.post("/model")
    @requires_auth(app, auth_token)
    async def load_model(request: Request):
        """Loads a zipped model, replacing the existing one."""

        if 'model' not in request.files:
            # model file is missing
            raise ErrorResponse(400, "InvalidParameter",
                                "You did not supply a model as part of your "
                                "request.",
                                {"parameter": "model", "in": "body"})

        model_file = request.files['model']

        logger.info("Received new model through REST interface.")
        zipped_path = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
        zipped_path.close()
        model_directory = tempfile.mkdtemp()

        model_file.save(zipped_path.name)

        logger.debug("Downloaded model to {}".format(zipped_path.name))

        zip_ref = zipfile.ZipFile(zipped_path.name, 'r')
        zip_ref.extractall(model_directory)
        zip_ref.close()
        logger.debug("Unzipped model to {}".format(
            os.path.abspath(model_directory)))

        domain_path = os.path.join(os.path.abspath(model_directory),
                                   "domain.yml")
        domain = Domain.load(domain_path)
        ensemble = PolicyEnsemble.load(model_directory)
        app.agent.update_model(domain, ensemble, None)
        logger.debug("Finished loading new agent.")
        return response.text('', 204)

    @app.post("/evaluate")
    @requires_auth(app, auth_token)
    async def evaluate_stories(request: Request):
        """Evaluate stories against the currently loaded model."""
        import rasa_nlu.utils

        tmp_file = rasa_nlu.utils.create_temporary_file(request.body,
                                                        mode='w+b')
        use_e2e = utils.bool_arg(request, 'e2e', default=False)
        try:
            evaluation = await test(tmp_file, app.agent, use_e2e=use_e2e)
            return response.json(evaluation)
        except ValueError as e:
            raise ErrorResponse(400, "FailedEvaluation",
                                "Evaluation could not be created. Error: {}"
                                "".format(e))

    @app.post("/conversations/handle-message-w-condition")
    @requires_auth(app, auth_token)
    @ensure_loaded_agent(app)
    async def handle_message_w_condition(request: Request):
        """ Handle the message for every user that would match the slot condition

        Returns
            responses: A jsonify dict of sender_ids on which the action trigger were call to
        """
        # retrieve parameters
        request_params = request.json
        message_to_handle = (request_params.get("name") or
                             request_params.get("message"))
        slot_name = request_params.get("slot_condition_name", "")
        slot_value = request_params.get("slot_value", None)
        force_update = request_params.get("force_update", False)
        # dict of responses that will store all the ids that were updated as keys
        responses = {}
        # Retrieve ids from tracker_store
        ids = retrieve_keys(app)
        try:
            # For each ids, handle the incoming message if it matches the condition
            # or update is forced
            for id in ids:
                logger.info('id: {}, format_id: {}'.format(id, type(id)))
                sender_id_str = re.findall('\+[0-9]+', id.decode('utf-8'))[0]
                logger.info('sender_id_str: {}, format_sender_id_str: {}'.format(sender_id_str, type(sender_id_str)))
                tracker = app.agent.tracker_store.get_or_create_tracker(sender_id_str)
                tracker_slot_value = tracker.get_slot(slot_name)
                if force_update or tracker_slot_value == slot_value:
                    output_channel = _get_output_channel(request, tracker)
                    logger.info('output_channel: {}'.format(output_channel))
                    await app.agent.handle_text(message_to_handle,
                                                output_channel=output_channel,
                                                sender_id=sender_id_str)
                    # add id to result
                    responses[id] = "Message {} handle from id {}".format(message_to_handle,
                                                                          sender_id_str)

        except ValueError as e:
            raise ErrorResponse(400, "ValueError", e)
        except Exception as e:
            logger.exception("Caught an exception during handle-message-w-condition.")
            raise ErrorResponse(500, "ActionException",
                                "Server failure. Error: {}".format(e))
        return response.json(responses)

    @app.post("/conversations/resume-dead-conversations")
    @requires_auth(app, auth_token)
    @ensure_loaded_agent(app)
    async def trigger_resume_inactive_conv(request: Request):
        """ Get the last event timestamp from a list of ids
        and trigger an action if their last event was made more than a day ago
        or if force_update is called

        Returns
            responses: A jsonify dict of sender_ids on which the action trigger were call to
        """
        # retrieve parameters
        request_params = request.json
        action_to_execute = (request_params.get("name") or
                             request_params.get("action"))
        policy = request_params.get("policy", None)
        confidence = request_params.get("confidence", None)
        force_update = request_params.get("force_update", False)
        verbosity = event_verbosity_parameter(request,
                                              EventVerbosity.AFTER_RESTART if not force_update
                                                                           else EventVerbosity.APPLIED)
        # dict of responses that will store all the ids that were updated as keys
        responses = {}
        # Retrieve ids from tracker_store
        ids = retrieve_keys(app)
        current_time = datetime.datetime.utcnow()
        # For each ids, trigger an action if it was stalled for more than a day 
        for id in ids:
            tracker = app.agent.tracker_store.get_or_create_tracker(id)
            id_state = tracker.current_state(verbosity)
            logger.debug('Retrieve tracker state')
            tracker_dialogue = tracker.as_dialogue()
            logger.debug('tracker as dialogue retrieved')
            new_tracker = app.agent.tracker_store.init_tracker(id)
            logger.debug('reinitilisation of tracker done')
            new_tracker.recreate_from_dialogue(tracker_dialogue)
            logger.debug('recreation of tracker from diag done')
            # tracker = app.agent.tracker_store.deserialise_tracker(id, app.agent.tracker_store.serialise_tracker(tracker))
            # check if the last event was made within the last 86400secs (24h)
            last_event_ts_from_id = datetime.datetime.utcfromtimestamp(id_state["latest_event_time"])
            seconds_in_a_day = 86400
            if force_update or compare_utcdatetime_with_timegap(current_time, last_event_ts_from_id, seconds_in_a_day):
                try:
                    output_channel = _get_output_channel(request, new_tracker)
                    logger.info('output_channel: {}'.format(output_channel))
                    await app.agent.execute_action(id,
                                                   action_to_execute,
                                                   output_channel,
                                                   policy,
                                                   confidence)
                    # add id to result
                    responses[id] = "Action trigger sent"

                except ValueError as e:
                    raise ErrorResponse(400, "ValueError", e)
                except Exception as e:
                    logger.error("Encountered an exception while running action '{}'. "
                                 "Bot will continue, but the actions events are lost. "
                                 "Make sure to fix the exception in your custom "
                                 "code.".format(action_to_execute))
                    logger.debug(e, exc_info=True)
                    raise ErrorResponse(500, "ValueError",
                                        "Server failure. Error: {}".format(e))
        return response.json(responses)

    @app.post("/jobs")
    @requires_auth(app, auth_token)
    async def train_stack(request: Request):
        """Train a Rasa Stack model."""

        from rasa.train import train_async

        rjs = request.json

        # create a temporary directory to store config, domain and
        # training data
        temp_dir = tempfile.mkdtemp()

        try:
            config_path = os.path.join(temp_dir, 'config.yml')
            dump_obj_as_str_to_file(config_path, rjs["config"])

            domain_path = os.path.join(temp_dir, 'domain.yml')
            dump_obj_as_str_to_file(domain_path, rjs["domain"])

            nlu_path = os.path.join(temp_dir, 'nlu.md')
            dump_obj_as_str_to_file(nlu_path, rjs["nlu"])

            stories_path = os.path.join(temp_dir, 'stories.md')
            dump_obj_as_str_to_file(stories_path, rjs["stories"])
        except KeyError:
            raise ErrorResponse(400,
                                "TrainingError",
                                "The Rasa Stack training request is "
                                "missing a key. The required keys are "
                                "`config`, `domain`, `nlu` and `stories`.")

        # the model will be saved to the same temporary dir
        # unless `out` was specified in the request
        try:
            model_path = await train_async(
                domain=domain_path,
                config=config_path,
                training_files=[nlu_path, stories_path],
                output=rjs.get("out", temp_dir),
                force_training=rjs.get("force", False))

            return await response.file(model_path)
        except Exception as e:
            raise ErrorResponse(400, "TrainingError",
                                "Rasa Stack model could not be trained. "
                                "Error: {}".format(e))

    @app.get("/domain")
    @requires_auth(app, auth_token)
    @ensure_loaded_agent(app)
    async def get_domain(request: Request):
        """Get current domain in yaml or json format."""

        accepts = request.headers.get("Accept", default="application/json")
        if accepts.endswith("json"):
            domain = app.agent.domain.as_dict()
            return response.json(domain)
        elif accepts.endswith("yml") or accepts.endswith("yaml"):
            domain_yaml = app.agent.domain.as_yaml()
            return response.text(domain_yaml,
                                 status=200,
                                 content_type="application/x-yml")
        else:
            raise ErrorResponse(406,
                                "InvalidHeader",
                                "Invalid Accept header. Domain can be "
                                "provided as "
                                "json (\"Accept: application/json\") or"
                                "yml (\"Accept: application/x-yml\"). "
                                "Make sure you've set the appropriate Accept "
                                "header.")

    @app.post("/finetune")
    @requires_auth(app, auth_token)
    @ensure_loaded_agent(app)
    async def continue_training(request: Request):
        epochs = request.raw_args.get("epochs", 30)
        batch_size = request.raw_args.get("batch_size", 5)
        request_params = request.json
        sender_id = UserMessage.DEFAULT_SENDER_ID

        try:
            tracker = DialogueStateTracker.from_dict(sender_id,
                                                     request_params,
                                                     app.agent.domain.slots)
        except Exception as e:
            raise ErrorResponse(400, "InvalidParameter",
                                "Supplied events are not valid. {}".format(e),
                                {"parameter": "", "in": "body"})

        try:
            # Fetches the appropriate bot response in a json format
            app.agent.continue_training([tracker],
                                        epochs=epochs,
                                        batch_size=batch_size)
            return response.text('', 204)

        except Exception as e:
            logger.exception("Caught an exception during prediction.")
            raise ErrorResponse(500, "TrainingException",
                                "Server failure. Error: {}".format(e))

    @app.get("/status")
    @requires_auth(app, auth_token)
    async def status(request: Request):
        return response.json({
            "model_fingerprint": app.agent.fingerprint if app.agent else None,
            "is_ready": app.agent.is_ready() if app.agent else False
        })

    @app.post("/predict")
    @requires_auth(app, auth_token)
    @ensure_loaded_agent(app)
    async def tracker_predict(request: Request):
        """ Given a list of events, predicts the next action"""

        sender_id = UserMessage.DEFAULT_SENDER_ID
        request_params = request.json
        verbosity = event_verbosity_parameter(request,
                                              EventVerbosity.AFTER_RESTART)

        try:
            tracker = DialogueStateTracker.from_dict(sender_id,
                                                     request_params,
                                                     app.agent.domain.slots)
        except Exception as e:
            raise ErrorResponse(400, "InvalidParameter",
                                "Supplied events are not valid. {}".format(e),
                                {"parameter": "", "in": "body"})

        policy_ensemble = app.agent.policy_ensemble
        probabilities, policy = \
            policy_ensemble.probabilities_using_best_policy(tracker,
                                                            app.agent.domain)

        scores = [{"action": a, "score": p}
                  for a, p in zip(app.agent.domain.action_names, probabilities)]

        return response.json({
            "scores": scores,
            "policy": policy,
            "tracker": tracker.current_state(verbosity)
        })

    @app.post("/parse")
    @requires_auth(app, auth_token)
    @ensure_loaded_agent(app)
    async def parse(request: Request):
        request_params = request.json
        parse_data = await app.agent.interpreter.parse(request_params.get("q"))
        return response.json(parse_data)

    async def _load_agent(
            model_path: Optional[Text] = None,
            model_server: Optional[EndpointConfig] = None,
            remote_storage: Optional[Text] = None,
            endpoints: Optional[AvailableEndpoints] = None,
            interpreter=None
    ) -> Agent:
        """
        Configures and returns a rasa core Agent. (Interface)
        Args:
            model_path: Path to a combined Rasa model.
            model_server: Access credentials of a model server.
            remote_storage: string reference to a cloud persistence storage solution.
            endpoints: Path to endpoints configuration yaml file.
            interpreter: n/a

        Returns
            loaded_agent: configured rasa core Agent. (Interface)
        """
        try:
            tracker_store = None
            generator = None
            action_endpoint = endpoints.action

            if endpoints:
                tracker_store = TrackerStore.find_tracker_store(
                    None, endpoints.tracker_store
                )
                action_endpoint = endpoints.action

            loaded_agent = await load_agent(
                model_path,
                model_server,
                remote_storage,
                interpreter=interpreter,
                generator=generator,
                tracker_store=tracker_store,
                action_endpoint=action_endpoint,
            )
        except Exception as e:
            logger.debug(e.args[0])
            raise ErrorResponse(
                500, "LoadingError", "An unexpected error occurred. Error: {}".format(e)
            )

        if not loaded_agent:
            raise ErrorResponse(
                400,
                "BadRequest",
                "Agent with name '{}' could not be loaded.".format(model_path),
                {"parameter": "model", "in": "query"},
            )

        return loaded_agent

    def validate_request_body(request: Request, error_message: Text):
        if not request.body:
            raise ErrorResponse(400, "BadRequest", error_message)

    @app.put("/model")
    @requires_auth(app, auth_token)
    async def load_model(request: Request):
        """
        Endpoint to trigger the fetch and load of a rasa core trained model ( in tar.gz form ) from
         an AWS remote storage service (S3)

        The /model endpoint expects a request with the following JSON payload parameters:

            - model_file:
            - model_server:
            - remote_storage:
            - endpoints: Path to endpoints file.
            - nlu_model:
            - credentials: Path to channel credentials file.

        """
        logger.debug("Received PUT request to /model endpoint... Loading new RASA core model from S3")

        validate_request_body(request, "No path to model file defined in request_body.")

        # Get params from request.
        model_path = request.json.get("model_file", None)
        model_server = request.json.get("model_server", None)
        remote_storage = request.json.get("remote_storage", None)
        endpoints = request.json.get("endpoints", None)
        nlu_model = request.json.get("nlu_model", None)
        logger.debug("PUT model request contains the following parameters: "
                     "model_file: {}, model_server: {}, remote_storage: {}, endpoints: {}, nlu_model {}".
                     format(model_path, model_server, remote_storage, endpoints, nlu_model))

        # Configure Endpoints
        nlu_endpoint = None
        _endpoints = AvailableEndpoints.read_endpoints(endpoints)
        if _endpoints.nlu:
            nlu_endpoint = _endpoints.nlu

        # Configure NLI
        _interpreter = NaturalLanguageInterpreter.create(nlu_model, nlu_endpoint)

        # Set app agent.
        app.agent = await \
            _load_agent(model_path, model_server, remote_storage, endpoints=_endpoints, interpreter=_interpreter)

        logger.debug("Successfully loaded model '{}'.".format(model_path))
        return response.json(None, status=204)

    @app.get("/conversations/<sender_id>/messages")
    @requires_auth(app, auth_token)
    async def retrieve_conversation_messages(request: Request, sender_id: Text):
        """Get only actual conversation messages ('user' and 'bot' events) from tracker."""

        if not app.agent.tracker_store:
            raise ErrorResponse(503, "NoTrackerStore",
                                "No tracker store available. Make sure to "
                                "configure a tracker store when starting "
                                "the server.")

        # parameters
        default_verbosity = EventVerbosity.ALL

        verbosity = event_verbosity_parameter(request,
                                              default_verbosity)

        # retrieve tracker and set to requested state
        tracker = app.agent.tracker_store.get_or_create_tracker(sender_id)
        if not tracker:
            raise ErrorResponse(503,
                                "NoDomain",
                                "Could not retrieve tracker. Most likely "
                                "because there is no domain set on the agent.")

        # get current state of a tracker and then extract events from it
        state = tracker.current_state(verbosity)
        all_events = state["events"]

        # retrieve only 'user' and 'bot' events from the complete list of events
        user_bot_kv_list = ["user", "bot"]
        all_user_bot_events = [e for e in all_events if e["event"] in user_bot_kv_list]

        # retrieve only 'events', 'timestamp' and 'text' part of the 'user' and 'bot' events
        user_bot_keys_list = ["event", "timestamp", "text"]
        user_bot_events = [dict((set_key_value(key), value) for key, value in a.items() if key in user_bot_keys_list)
                           for a in all_user_bot_events]

        # get rid of the event dictionaries where 'text' part is None (null)
        user_bot_events_final = [e for e in user_bot_events if e["text"]]

        # send the list of final events as a response
        return response.json({"events": user_bot_events_final})

    return app

def retrieve_keys(app):
    """ Retrieves the list of keys of the tracker store

    Returns:
        list -- tracker_store list of keys
    """
    keys = []
    if not app.agent.tracker_store:
        raise ErrorResponse(503, "NoTrackerStore",
                            "No tracker store available. Make sure to "
                            "configure a tracker store when starting "
                            "the server.")
    else:
        keys = app.agent.tracker_store.keys()
    return list(keys) if keys else []

def compare_utcdatetime_with_timegap(dt_a, dt_b, gap):
    """Check the timegap between two datetimes

    Arguments:
        dt_a {UTCdatetime} -- Time A to compared
        dt_b {UTCdatetime} -- Time B to compared
        gap {int} -- gap to considered in seconds

    Returns:
        Bool -- True if dt_a is more recent than dt_b + gap
    """
    return ((dt_a - dt_b).total_seconds() >= gap)

def set_key_value(key):
    if key == "event":
        return "author"
    else:
        return key


def _get_output_channel(
    request: Request, tracker: Optional[DialogueStateTracker]
    ) -> OutputChannel:
    """Returns the `OutputChannel` which should be used for the bot's responses.
    Args:
        request: HTTP request whose query parameters can specify which `OutputChannel`
                 should be used.
        tracker: Tracker for the conversation. Used to get the latest input channel.
    Returns:
        `OutputChannel` which should be used to return the bot's responses to.
    """
    requested_output_channel = request.args.get(OUTPUT_CHANNEL_QUERY_KEY)

    if (
        requested_output_channel == USE_LATEST_INPUT_CHANNEL_AS_OUTPUT_CHANNEL
        and tracker
    ):
        requested_output_channel = tracker.get_latest_input_channel()

    # Interactive training does not set `input_channels`, hence we have to be cautious
    registered_input_channels = request.app.config.get("input_channels")

    matching_channels = [
        channel
        for channel in registered_input_channels
        if channel.name() == requested_output_channel
    ]

    # Check if matching channels can provide a valid output channel,
    # otherwise use `CollectingOutputChannel`
    return reduce(
        lambda output_channel_created_so_far, input_channel: (
            input_channel.get_output_channel() or output_channel_created_so_far
        ),
        matching_channels,
        CollectingOutputChannel(),
    )

if __name__ == '__main__':
    raise RuntimeError("Calling `rasa_core.server` directly is "
                       "no longer supported. "
                       "Please use `rasa_core.run --enable_api` instead.")
