"""Shared output-language rule for all notebook generation.

Every notebook prompt that produces user-facing text (artifacts, podcast,
video, grounded chat, question suggestions) embeds this single rule so the
output language cannot drift per feature. Change it here, never inline.
"""

DUTCH_OUTPUT_RULE = (
    "Schrijf altijd in het Nederlands, ongeacht de taal van de bronnen of van "
    "de vraag. Vertaal informatie uit anderstalige bronnen naar natuurlijk "
    "Nederlands; alleen letterlijke citaten en vaktermen zonder gangbare "
    "Nederlandse vertaling blijven in de oorspronkelijke taal."
)
