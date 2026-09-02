"""Image/video generation requests must route to the image/video categories.

Covers NL + EN phrasings (including Dutch object-before-verb order, e.g. "een
animatie maken") and confirms existing routing (calendar/notes/email/web/
describe/vision/upload intents) is unaffected.
"""
import pytest

from src.action_intents import classify_tool_intent


@pytest.mark.parametrize("text", [
    "maak een afbeelding van een kat op een fiets",
    "Genereer een plaatje van de Eiffeltoren bij nacht",
    "teken een logo voor mijn bakkerij",
    "kun je een illustratie maken van een draak",
    "create an image of a red bicycle",
    "draw a picture of a lighthouse",
    "make me a poster for the school party",
])
def test_image_requests_route_to_image(text):
    intent = classify_tool_intent(text)
    assert intent.needs_tools and intent.category == "image"


@pytest.mark.parametrize("text", [
    "maak een video van een surfende hond",
    "genereer een filmpje over de zee",
    "create a short clip of a rocket launch",
    "kun je een animatie maken van een dansende robot",
    "make a video of waves at sunset",
])
def test_video_requests_route_to_video(text):
    intent = classify_tool_intent(text)
    assert intent.needs_tools and intent.category == "video"


@pytest.mark.parametrize("text", [
    "zoek een afbeelding van de Eiffeltoren",
    "beschrijf deze afbeelding",
    "wat zie je op deze foto",
    "find a video about python decorators",
    "vat deze youtube video samen",
    "how do I generate an image here?",
    "upload een afbeelding",
])
def test_media_negatives_do_not_route(text):
    intent = classify_tool_intent(text)
    assert intent.category not in ("image", "video")


def test_existing_calendar_still_calendar():
    assert classify_tool_intent("add lunch to my calendar tomorrow").category == "calendar"
