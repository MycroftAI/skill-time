# Copyright 2021, Mycroft AI Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Mycroft skill for reporting the current and future time.

NOTE ON REGULAR EXPRESSION HANDLING:
    Most skills that have regular expression files (those with a ".rx" extension) can
    use the intent system to determine if words matching the regular expression(s) are
    present in the utterance.  This has the benefit of improving intent confidence
    calculations.  This skill, however, foregoes that mechanism.

    Consider the following request from the user:
        What time is it in London in 5 hours?

    A regular expression to find the location in this request is difficult to write.
    The location can be found looking of the word "in" followed by a city name.  But in
    this case, the future offset "in 5 hours" would also match this type of pattern.

    To get around this situation, this skill does not pass the regular expression on to
    Adapt.  Instead, it pulls the future offset out of the user's request first,
    leaving only the city to match the regular expression looking for a location.
"""
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from mycroft.audio import wait_while_speaking
from mycroft.messagebus.message import Message
from mycroft.skills import MycroftSkill, intent_handler
from mycroft.skills.intent_service import AdaptIntent
from mycroft.util.format import date_time_format
from mycroft.util.time import now_local
from .skill import FaceplateRenderer, get_display_time, LocationNotFoundError, Response

MARK_I = "mycroft_mark_1"
MARK_II = "mycroft_mark_2"
TEN_SECONDS = 10


class TimeSkill(MycroftSkill):
    """Mycroft skill for reporting the current and future time."""

    def __init__(self):
        super().__init__("TimeSkill")
        self.displayed_time = None
        self.location_regex_path = Path(self.find_resource("location.rx"))

    # TODO: define in the base class so it can be accessed by other skills.
    @property
    def platform(self) -> Optional[str]:
        """Get the platform identifier string

        Returns:
            Platform identifier (e.g. "mycroft_mark_1", "mycroft_picroft",
            "mycroft_mark_2") or None for non-standard platform.
        """
        platform = None
        if self.config_core and self.config_core.get("enclosure"):
            platform = self.config_core["enclosure"].get("platform")

        return platform

    # TODO: Move the Mark I functionality for idle screens into the Home Screen skill
    @property
    def display_when_idle(self):
        """Syntactic sugar for whether or not the time should be displayed when idle."""
        show_time_when_idle = False
        if self.platform == MARK_I:
            show_time_when_idle = self.settings.get("show_time", False)

        return show_time_when_idle

    def initialize(self):
        """Do the things after the constructor but before logic is executed."""
        date_time_format.cache(self.lang)
        self._define_event_handlers()
        self._check_mark_i_idle_setting()
        if self.platform != MARK_I:
            self.disable_intent("mark-one-idle")

        self._current_time_cache_key = f"{self.skill_id}.current-time"

        self.add_event("mycroft.started", self._cache_current_time_tts)

    def _define_event_handlers(self):
        """Define the events this skill will handle and the associated callback."""
        self.add_event("recognizer_loop:wakeword", self.handle_wake_word_detected)
        self.add_event("speak", self.handle_speak)
        self.add_event(
            "mycroft.speech.recognition.unknown", self.handle_speech_recognition_unknown
        )

    @intent_handler(AdaptIntent().require("query").require("time"))
    def handle_current_time_adapt(self, request: Message):
        """Respond to a request for the current time (e.g. "what time is it?")

        Args:
            request: Data from the intent parser regarding the user's voice request.
        """
        with self.activity():
            self._handle_current_time(request)

    @intent_handler("what-time-is-it.intent")
    def handle_current_time_padatious(self, request: Message):
        """Respond to a less common request for the current time.

        Example: "do you have the time?"

        Args:
            request: Data from the intent parser regarding the user's voice request.
        """
        with self.activity():
            self._handle_current_time(request)

    @intent_handler(
        AdaptIntent("")
        .optionally("query")
        .require("time")
        .require("future")
        .require("duration")
    )
    def handle_future_time_adapt(self, request: Message):
        """Respond to a request for the future time.

        Example: "What time will it be in 8 hours?"

        Args:
            request: Data from the intent parser regarding the user's voice request.
        """
        with self.activity():
            self._handle_future_time(request)

    @intent_handler("what-time-will-it-be.intent")
    def handle_future_time_padatious(self, request: Message):
        """Respond to a less common request for the future time.

        Example: "when is it 8 hours from now?"

        Args:
            request: Data from the intent parser regarding the user's voice request.
        """
        with self.activity():
            self._handle_future_time(request)

    @intent_handler(AdaptIntent("mark-one-idle").require("display").require("time"))
    def handle_show_time(self, _):
        """Respond to a request top show the time on a Mark I when idle.

        Example: "What time will it be in 8 hours?"
        """
        with self.activity():
            self.settings["show_time"] = True
            self._check_mark_i_idle_setting()

    def _handle_future_time(self, request: Message):
        """Respond to a request for the future time.

        Determine the future time, then speak the result to the user and display
        it (if applicable).

        Args:
            request: Data from the intent parser regarding the user's voice request.
        """
        response = Response(self.config_core, self.location_regex_path)
        try:
            response.build_future_time_response(request.data["utterance"])
        except LocationNotFoundError:
            self._handle_location_not_found(response)
        else:
            if response.date_time is None:
                self._handle_current_time(request)
            else:
                self._respond(response)

    def _handle_current_time(self, request: Message):
        """Respond to a request for the current time.

        Determine the current time, then speak the result to the user and display
        it (if applicable).

        Args:
            request: Data from the intent parser regarding the user's voice request.
        """
        self.log.info("request data: " + str(request.data.keys()))
        response = Response(self.config_core, self.location_regex_path)
        try:
            response.build_current_time_response(request.data["utterance"])
        except LocationNotFoundError:
            self._handle_location_not_found(response)
        else:
            cache_key = (
                self._current_time_cache_key
                if not response.requested_location
                else None
            )
            self._respond(response, cache_key=cache_key)
            self._cache_current_time_tts()

    def _handle_location_not_found(self, response: Response):
        """User requested time in a city not recognized by a Geolocation API call.

        Args:
            response: object used to formulate the response
        """
        dialog_data = dict(location=response.requested_location)
        self.speak_dialog("location-not-found", dialog_data)

    def _respond(self, response: Response, cache_key=None):
        """Speak and display the response to the user's request.

        Args:
            response: object used to formulate the response
        """
        self._display_time(response)
        self.speak_dialog(
            response.dialog_name, response.dialog_data, cache_key=cache_key, wait=True
        )
        if self.platform == MARK_I:
            self._clear_mark_i_display(delay=TEN_SECONDS)
        elif self.gui.connected:
            self.gui.release()

    def _display_time(self, response: Response):
        """Display the time on the appropriate medium for the active platform.

        Args:
            response: object used to formulate the response
        """
        if self.platform == MARK_I:
            self.enclosure.deactivate_mouth_events()
            display_time = get_display_time(response.date_time, self.config_core)
            self._display_mark_i(display_time)
        elif self.gui.connected:
            self._display_gui(response)

    def _display_mark_i(self, display_time: str):
        """Map time characters to the display encoding for a Mark I

        Args:
            display_time: the time to display on the faceplate
        """
        renderer = FaceplateRenderer(self.enclosure, display_time)
        alarm_is_set = self._is_alarm_set() if self.display_when_idle else False
        renderer.render_time(alarm_is_set)

    # TODO: change this to use the skill API
    # TODO: move this to the home screen skill
    def _is_alarm_set(self) -> bool:
        """Query the alarm skill if an alarm is set."""
        query = Message("private.mycroftai.has_alarm")
        msg = self.bus.wait_for_response(query)
        return msg and msg.data.get("active_alarms", 0) > 0

    def _display_gui(self, response: Response):
        """Display time on a device that supports the Mycroft GUI Framework.

        Args:
            response: object used to formulate the response
        """
        display_time = get_display_time(response.date_time, self.config_core)
        if self.platform == MARK_II:
            page_name = "time-mark-ii.qml"
            hour, minute = display_time.split(":")
            self.gui["hour"] = hour
            self.gui["minute"] = minute
            if response.geolocation is not None:
                self.gui["location"] = response.get_display_location()
        else:
            page_name = "time-scalable.qml"
            self.gui["timeString"] = display_time
        self.gui.show_page(page_name, override_idle=True)

    def handle_wake_word_detected(self, _):
        """Clear the display on the Mark I when a wake word is detected.

        This allows the listening and thinking animations on the Mark I faceplate to
        be visible.
        """
        if self.platform == MARK_I and self.display_when_idle:
            self._clear_mark_i_display()
            self.cancel_scheduled_event("IdleTime")

    def handle_speak(self, _):
        """After the device is done speaking, restore the time display on the Mark I.

        This allows for the Mark I's "mouth" to speak the response to a query before
        returning to the idle screen.
        """
        if self.platform == MARK_I and self.display_when_idle:
            wait_while_speaking()
            time.sleep(5)
            self._check_mark_i_idle_setting()

    def handle_speech_recognition_unknown(self, _):
        """After the device is done speaking, restore the time display on the Mark I.

        This allows for the Mark I's "mouth" to speak the response to a query before
        returning to the idle screen.
        """
        self._check_mark_i_idle_setting()

    # TODO: Move the Mark I functionality for idle screens into the Home Screen skill
    def _check_mark_i_idle_setting(self):
        """Start an event to display the time when idle if the setting is selected.

        This setting is only applicable to the Mark I.
        """
        if self.display_when_idle and self.platform == MARK_I:
            self.schedule_repeating_event(
                self.display_idle,
                when=now_local(),
                frequency=TEN_SECONDS,
                name="IdleTime",
            )

    # TODO: move to home screen skill
    def display_idle(self):
        """Display the time on the Mark I faceplate when idle."""
        display_is_idle = not self.enclosure.display_manager.get_active()
        if self.display_when_idle and display_is_idle:
            display_time = get_display_time(now_local(), self.config_core)
            if display_time != self.displayed_time:
                self.displayed_time = display_time
                self._display_mark_i(display_time)
                self.enclosure.display_manager.remove_active()

    def _clear_mark_i_display(self, delay=0):
        """After the time has been displayed, clear the Mark I faceplate.

        Args:
            delay: the number of seconds to wait before clearing the faceplate.
        """
        if self.platform == MARK_I:
            time.sleep(delay)
            self.enclosure.mouth_reset()
            self.enclosure.activate_mouth_events()
            self.enclosure.display_manager.remove_active()

    def load_regex_files(self):
        """Skip this logic to handle the location regular expression in the skill.

        See note in module-level docstring.
        """
        pass

    def _cache_current_time_tts(self, _message=None):
        try:
            # Re-cache in a minute
            self.cancel_scheduled_event("CacheTTS")

            now = datetime.now()
            next_minute = datetime(
                year=now.year,
                month=now.month,
                day=now.day,
                hour=now.hour,
                minute=now.minute,
            ) + timedelta(minutes=1)

            self.schedule_event(
                self._cache_current_time_tts, when=next_minute, name="CacheTTS"
            )

            response = Response(self.config_core, self.location_regex_path)
            response.build_current_time_response("")

            self.cache_dialog(
                response.dialog_name,
                response.dialog_data,
                cache_key=self._current_time_cache_key,
            )
        except Exception:
            self.log.exception("Error while caching TTS")


def create_skill():
    """Boilerplate code used to load the skill."""
    return TimeSkill()
