"""The voice model over the Live API, for typed answers, seeing and moves."""

import json

import pytest

from carnage.livetext import LiveConversation, LiveError, LiveModel, trouble


class Socket:
    """The Live API's side of the websocket, scripted.

    `replies` answers what the client sends, in order: each entry is the list
    of frames the server sends back after that client frame.
    """

    def __init__(self, replies):
        self.replies = [list(r) for r in replies]
        self.sent = []
        self.queue = []

    def send(self, raw):
        self.sent.append(json.loads(raw))
        if self.replies:
            self.queue.extend(json.dumps(f) for f in self.replies.pop(0))

    def recv(self, timeout=None):
        if not self.queue:
            raise TimeoutError
        return self.queue.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def said(text, done=False):
    content = {"outputTranscription": {"text": text}}
    if done:
        content["turnComplete"] = True
    return {"serverContent": content}


def model_with(socket, **kw):
    return LiveModel("KEY", "models/voice", connect=lambda url: socket, **kw)


def test_a_typed_question_gets_the_transcript_of_her_spoken_answer():
    socket = Socket([[{"setupComplete": {}}],
                     [said("Ye Galaxy "), said("A17 hai."), said("", done=True)]])
    assert model_with(socket).run([{"role": "user", "parts": [{"text": "kaunsa phone?"}]}],
                                  system="persona") == "Ye Galaxy A17 hai."
    setup = socket.sent[0]["setup"]
    assert setup["model"] == "models/voice"
    assert setup["generationConfig"]["responseModalities"] == ["AUDIO"]
    assert "outputAudioTranscription" in setup
    assert socket.sent[1]["clientContent"]["turnComplete"] is True


def test_tool_calls_are_answered_and_she_speaks_after_the_result():
    socket = Socket([
        [{"setupComplete": {}}],
        [{"toolCall": {"functionCalls": [{"id": "1", "name": "where_am_i", "args": {}}]}},
         {"serverContent": {"turnComplete": True}}],     # a turn of only the call
        [said("You're on MG Road."), said("", done=True)],
    ])
    calls = []

    def answer(name, args):
        calls.append(name)
        return "MG Road, Bengaluru"

    text = model_with(socket).run([{"role": "user", "parts": [{"text": "where am I"}]}],
                                  functions=[{"name": "where_am_i"}], on_call=answer)
    assert text == "You're on MG Road."
    assert calls == ["where_am_i"]
    reply = socket.sent[2]["toolResponse"]["functionResponses"][0]
    assert reply == {"id": "1", "name": "where_am_i",
                     "response": {"result": "MG Road, Bengaluru"}}


def test_a_move_is_the_arguments_of_her_first_call():
    socket = Socket([[{"setupComplete": {}}],
                     [{"toolCall": {"functionCalls": [
                         {"id": "9", "name": "move", "args": {"action": "tap", "id": 4}}]}}]])
    move = model_with(socket).decide("screen…", "system", {"name": "move"})
    assert move == {"action": "tap", "id": 4}
    assert len(socket.sent) == 2                   # no tool response: done at the call


def test_seeing_sends_the_picture_in_the_turn():
    socket = Socket([[{"setupComplete": {}}], [said("A cup of chai.", done=True)]])
    assert model_with(socket).describe("what is it", "JPEGDATA") == "A cup of chai."
    parts = socket.sent[1]["clientContent"]["turns"][0]["parts"]
    assert parts[0] == {"inlineData": {"mimeType": "image/jpeg", "data": "JPEGDATA"}}


def test_search_switches_on_google_search():
    socket = Socket([[{"setupComplete": {}}], [said("It's 31°C.", done=True)]])
    assert model_with(socket).search("weather in Pune") == "It's 31°C."
    assert {"googleSearch": {}} in socket.sent[0]["setup"]["tools"]


def test_silence_is_a_timeout_not_a_hang():
    socket = Socket([[{"setupComplete": {}}], []])
    with pytest.raises(LiveError, match="too long"):
        model_with(socket).run([{"role": "user", "parts": [{"text": "hi"}]}])


def test_a_used_up_quota_is_said_plainly():
    assert "quota is used up" in trouble(LiveError("closed: RESOURCE_EXHAUSTED quota"))
    assert "couldn't reach" in trouble(LiveError("network down"))


def test_a_conversation_that_fails_says_why_and_forgets_nothing_it_did_not_say():
    class Broken:
        def run(self, *_, **__):
            raise LiveError("the voice model closed the session: quota exceeded")

    class Registry:
        def gemini_declarations(self, uppercase_types=False):
            return []

    talk = LiveConversation(Broken(), Registry(), lambda: "persona")
    assert "quota is used up" in talk.ask("hello")
    assert talk.history == []


def test_her_words_are_passed_on_as_they_arrive():
    socket = Socket([[{"setupComplete": {}}],
                     [said("Ye "), said("Samsung "), said("hai."), said("", done=True)]])
    heard = []
    model_with(socket).run([{"role": "user", "parts": [{"text": "?"}]}], on_text=heard.append)
    assert heard == ["Ye", "Ye Samsung", "Ye Samsung hai."]


def test_a_shared_file_goes_in_the_same_turn_as_the_question():
    class Model:
        def run(self, turns, **options):
            self.turns, self.options = turns, options
            return "It's a bill."

    class Registry:
        def gemini_declarations(self, uppercase_types=False):
            return []

    model, words = Model(), []
    LiveConversation(model, Registry(), lambda: "p").ask(
        "what is this?", "context", [{"mime": "application/pdf", "data": "PDF"}], words.append)
    parts = model.turns[-1]["parts"]
    assert parts[0] == {"inlineData": {"mimeType": "application/pdf", "data": "PDF"}}
    assert [p.get("text") for p in parts[1:]] == ["context", "what is this?"]
    assert model.options["on_text"] == words.append
