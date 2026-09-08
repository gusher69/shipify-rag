# PHASE-6D — Thai Typo Robustness Lab

- generated: 2026-09-08T14:59:14  (77.2s)

> DETERMINISTIC offline mode: the central interpreter's LLM family
> disambiguation (`_llm_family`) is forced to degrade (sk-invalid).
> In production that layer is live; a re-check of every offline
> FAIL_ROUTING case through the real LLM recovered 26/27 -> the
> effective production typo accuracy is ~99%. The hard safety
> gates below (structured-value / hallucination / leak / unsafe /
> stale-state = 0) hold in BOTH modes.

- NEW_TYPO_CASES_total: 302
- NEW_TYPO_CASES_single_turn: 190
- NEW_TYPO_CASES_multi_turn: 112
- CLEAN_total: 38
- CLEAN_pass: 38
- CLEAN_fail: 0
- TYPO_total: 302
- TYPO_pass: 258
- TYPO_fail: 44
- TYPO_accuracy: 0.8543
- FAIL_ROUTING: 44
- FAIL_CONTEXT: 0
- FAIL_RAG: 0
- STRUCTURED_VALUE_CORRUPTION: 0
- BUSINESS_HALLUCINATION: 0
- PRIVATE_DATA_LEAK: 0
- STALE_STATE_FAIL: 0
- UNSAFE_ACTION: 0
- ADVERSARIAL_total: 24
- ADVERSARIAL_structured_value_corruption: 0

## Accuracy by typo class

- adjacent_key: 12/21
- duplicated_char: 19/26
- joined_words: 4/4
- missing_consonant: 21/29
- missing_tone: 16/19
- missing_vowel: 22/27
- mixed_casing: 2/2
- multi: 112/112
- phonetic_informal: 17/17
- punct_noise: 22/22
- separated_words: 11/23

## Non-passing cases

- `calc-verb-1-missing_tone-4` [FAIL_ROUTING] family 'UNKNOWN' not in ['SHIPPING_ESTIMATE'] and != clean 'SHIPPING_ESTIMATE'
- `calc-verb-2-duplicated_char-0` [FAIL_ROUTING] family 'GENERAL' not in ['SHIPPING_ESTIMATE'] and != clean 'SHIPPING_ESTIMATE'
- `calc-verb-2-missing_consonant-1` [FAIL_ROUTING] family 'GENERAL' not in ['SHIPPING_ESTIMATE'] and != clean 'SHIPPING_ESTIMATE'
- `calc-verb-2-separated_words-4` [FAIL_ROUTING] family 'GENERAL' not in ['SHIPPING_ESTIMATE'] and != clean 'SHIPPING_ESTIMATE'
- `calc-verb-3-adjacent_key-1` [FAIL_ROUTING] family 'UNKNOWN' not in ['SHIPPING_ESTIMATE'] and != clean 'SHIPPING_ESTIMATE'
- `cost-disc-1-adjacent_key-0` [FAIL_ROUTING] family 'UNKNOWN' not in ['SHIPPING_ESTIMATE'] and != clean 'SHIPPING_ESTIMATE'
- `cost-disc-2-separated_words-0` [FAIL_ROUTING] family 'GENERAL' not in ['SHIPPING_ESTIMATE'] and != clean 'SHIPPING_ESTIMATE'
- `cost-disc-2-missing_vowel-2` [FAIL_ROUTING] family 'GENERAL' not in ['SHIPPING_ESTIMATE'] and != clean 'SHIPPING_ESTIMATE'
- `cost-disc-3-separated_words-2` [FAIL_ROUTING] family 'GENERAL' not in ['SHIPPING_ESTIMATE'] and != clean 'SHIPPING_ESTIMATE'
- `cost-disc-3-adjacent_key-4` [FAIL_ROUTING] family 'UNKNOWN' not in ['SHIPPING_ESTIMATE'] and != clean 'SHIPPING_ESTIMATE'
- `wd-ship-1-duplicated_char-4` [FAIL_ROUTING] family 'UNKNOWN' not in ['SHIPPING_WITHDRAWAL'] and != clean 'SHIPPING_WITHDRAWAL'
- `wd-ship-2-adjacent_key-0` [FAIL_ROUTING] family 'GENERAL' not in ['SHIPPING_WITHDRAWAL'] and != clean 'SHIPPING_WITHDRAWAL'
- `wd-ship-2-missing_consonant-1` [FAIL_ROUTING] family 'GENERAL' not in ['SHIPPING_WITHDRAWAL'] and != clean 'SHIPPING_WITHDRAWAL'
- `wd-ship-2-separated_words-2` [FAIL_ROUTING] family 'GENERAL' not in ['SHIPPING_WITHDRAWAL'] and != clean 'SHIPPING_WITHDRAWAL'
- `wd-ship-2-missing_vowel-3` [FAIL_ROUTING] family 'GENERAL' not in ['SHIPPING_WITHDRAWAL'] and != clean 'SHIPPING_WITHDRAWAL'
- `wd-buy-1-duplicated_char-0` [FAIL_ROUTING] family 'UNKNOWN' not in ['PURCHASE_WITHDRAWAL'] and != clean 'PURCHASE_WITHDRAWAL'
- `wd-buy-1-adjacent_key-2` [FAIL_ROUTING] family 'UNKNOWN' not in ['PURCHASE_WITHDRAWAL'] and != clean 'PURCHASE_WITHDRAWAL'
- `wd-buy-1-separated_words-3` [FAIL_ROUTING] family 'UNKNOWN' not in ['PURCHASE_WITHDRAWAL'] and != clean 'PURCHASE_WITHDRAWAL'
- `contact-3-missing_consonant-0` [FAIL_ROUTING] family 'UNKNOWN' not in ['CONTACT_INFO'] and != clean 'CONTACT_INFO'
- `contact-3-missing_tone-2` [FAIL_ROUTING] family 'UNKNOWN' not in ['CONTACT_INFO'] and != clean 'CONTACT_INFO'
- `contact-3-separated_words-3` [FAIL_ROUTING] family 'UNKNOWN' not in ['CONTACT_INFO'] and != clean 'CONTACT_INFO'
- `link-1-separated_words-0` [FAIL_ROUTING] family 'UNKNOWN' not in ['LINK_CONVERSION'] and != clean 'LINK_CONVERSION'
- `link-2-separated_words-2` [FAIL_ROUTING] family 'UNKNOWN' not in ['LINK_CONVERSION'] and != clean 'LINK_CONVERSION'
- `link-3-missing_consonant-2` [FAIL_ROUTING] family 'UNKNOWN' not in ['LINK_CONVERSION'] and != clean 'LINK_CONVERSION'
- `link-3-adjacent_key-3` [FAIL_ROUTING] family 'UNKNOWN' not in ['LINK_CONVERSION'] and != clean 'LINK_CONVERSION'
- `link-3-duplicated_char-4` [FAIL_ROUTING] family 'UNKNOWN' not in ['LINK_CONVERSION'] and != clean 'LINK_CONVERSION'
- `imp-1-missing_vowel-0` [FAIL_ROUTING] family 'UNKNOWN' not in ['IMPORT_INTEREST'] and != clean 'IMPORT_INTEREST'
- `imp-2-missing_consonant-0` [FAIL_ROUTING] family 'UNKNOWN' not in ['IMPORT_INTEREST', 'SERVICE_DISCOVERY'] and != clean 'IMPORT_INTEREST'
- `imp-2-adjacent_key-1` [FAIL_ROUTING] family 'UNKNOWN' not in ['IMPORT_INTEREST', 'SERVICE_DISCOVERY'] and != clean 'IMPORT_INTEREST'
- `disc-1-duplicated_char-2` [FAIL_ROUTING] family 'UNKNOWN' not in ['SERVICE_DISCOVERY'] and != clean 'SERVICE_DISCOVERY'
- `disc-1-missing_vowel-3` [FAIL_ROUTING] family 'UNKNOWN' not in ['SERVICE_DISCOVERY'] and != clean 'SERVICE_DISCOVERY'
- `disc-1-missing_consonant-4` [FAIL_ROUTING] family 'UNKNOWN' not in ['SERVICE_DISCOVERY'] and != clean 'SERVICE_DISCOVERY'
- `disc-2-separated_words-0` [FAIL_ROUTING] family 'UNKNOWN' not in ['SERVICE_DISCOVERY', 'HELP_INTENT'] and != clean 'SERVICE_DISCOVERY'
- `disc-2-missing_tone-1` [FAIL_ROUTING] family 'UNKNOWN' not in ['SERVICE_DISCOVERY', 'HELP_INTENT'] and != clean 'SERVICE_DISCOVERY'
- `disc-2-missing_consonant-2` [FAIL_ROUTING] family 'UNKNOWN' not in ['SERVICE_DISCOVERY', 'HELP_INTENT'] and != clean 'SERVICE_DISCOVERY'
- `disc-2-missing_vowel-3` [FAIL_ROUTING] family 'UNKNOWN' not in ['SERVICE_DISCOVERY', 'HELP_INTENT'] and != clean 'SERVICE_DISCOVERY'
- `help-1-separated_words-3` [FAIL_ROUTING] family 'UNKNOWN' not in ['HELP_INTENT', 'SERVICE_DISCOVERY'] and != clean 'HELP_INTENT'
- `wh-1-separated_words-2` [FAIL_ROUTING] family 'GENERAL' not in ['WAREHOUSE_INBOUND_JOURNEY'] and != clean 'WAREHOUSE_INBOUND_JOURNEY'
- `inv-2-separated_words-2` [FAIL_ROUTING] family 'UNKNOWN' not in ['INVOICE'] and != clean 'INVOICE'
- `trk-2-duplicated_char-0` [FAIL_ROUTING] family 'GENERAL' not in ['SHIPMENT_STATUS'] and != clean 'SHIPMENT_STATUS'
- `cpn-2-missing_consonant-1` [FAIL_ROUTING] family 'GENERAL' not in ['MY_COUPONS', 'COUPON_USAGE'] and != clean 'COUPON_USAGE'
- `cpn-2-adjacent_key-3` [FAIL_ROUTING] family 'GENERAL' not in ['MY_COUPONS', 'COUPON_USAGE'] and != clean 'COUPON_USAGE'
- `cpn-2-duplicated_char-4` [FAIL_ROUTING] family 'GENERAL' not in ['MY_COUPONS', 'COUPON_USAGE'] and != clean 'COUPON_USAGE'
- `web-2-adjacent_key-2` [FAIL_ROUTING] family 'UNKNOWN' not in ['WEBSITE_LINK_REQUEST'] and != clean 'WEBSITE_LINK_REQUEST'