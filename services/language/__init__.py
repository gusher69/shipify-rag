# -*- coding: utf-8 -*-
"""Human-language input layer (PLATFORM component).

One shared normalisation layer for noisy human text — typos, missing /
extra spaces, repeated vowels and tone marks, informal particles,
keyboard-layout slips — that every semantic layer consumes BEFORE it
reads the customer's message. The customer's raw text is never
overwritten; the layer returns both.

    services.language.thai_normalizer   the ONE normaliser
    services.language.vocabulary        bounded, per-deployment
                                        vocabularies the fuzzy matcher
                                        is allowed to correct towards

Customer configuration lives in vocabulary.py (a second tenant supplies
its own bounded vocabulary); the algorithm itself is language-family
specific (Thai script) but tenant-neutral.
"""
from services.language.thai_normalizer import (  # noqa: F401
    NormalizationResult, Candidate, normalize, normalize_history,
    protect_identifiers, mask_identifiers, skeleton, is_dictionary_word,
    oov_count,
)
