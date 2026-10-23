# How the current conversation works

Luna performed a read-only source audit for this explanation. This was an explanation task with code access, separate from the earlier blind verification. No model enquiry or new test suite was run for the explanation.

## Reply flow

The browser submits the current question. The API commits its source/turn/run/outbox before acknowledging acceptance. A worker calls the configured LLM for a structured plan selecting list_accounts, get_account, list_transactions, clarify or unsupported. Authorized SQL reads produce current bank data. Deterministic code checks and formats the financial answer. Safe errors and clarification wording are fixed strings.

The normal configured runtime uses the real LLM seam. The acceptance tests injected scripted raw model responses; those results do not certify live model quality. The bank balances themselves are database values, not hardcoded answer amounts. Wording templates, supported tools and safe replies are hardcoded.

Stored conversation history is available to the UI. The coordinator currently sends the current request to the model, without previous conversation turns. Natural follow-up resolution is therefore incomplete; an explicit canonical account reference is currently needed for an account-specific tool call.

## Preferences

Manual selectors save exact settings. The separate Preference instruction box also accepts constrained phrases, such as Always reply in Hindi or Always be concise. This path uses a fixed grammar, not LLM extraction. The ordinary account-question path does not call that capture endpoint. Preferences can be captured from conversation, but that integration is incomplete in the current account chat. Automatic extraction must still distinguish explicit user instructions and temporary scope from quotations, past preferences or third-party text.

## Languages

SPEC04 in the authoritative preference contract explicitly closes the language set to en/hi and format to concise/detailed. The API validators, UI choices, grammar and checked rendering follow those sets. Hindi currently uses a small set of fixed phrases/substitutions, not unrestricted translation. This is a product/contract restriction rather than a statement about the LLM's language ability. Expanding it requires an explicit contract change and verified rendering.

## Demo data added for browser checks

The earlier tests used different synthetic test owners. The T03.3 isolated fixture retained3 owners with4 accounts. Browser-login customer-a/customer-b initially had0 accounts. The user requested usable accounts, so3 clearly labelled INR demo accounts were added to these local users. See evidence/T03.3-manual-demo-accounts.json. Existing accounts and legacy rows were not rewritten. Credentials remain in ignored local configuration and are not copied into this document.

Sources: graphs/account_coordinator.py (planning and checked rendering), llm/config.py (real versus injected transport), store/banking_turns.py (current request/preferences), jobs/banking_turns.py, api/preferences.py, store/preference_grammar.py, frontend/components/PreferencePanel.tsx and .genesis/sources/P02_SOURCE.md (SPEC04). T03.3 human review remains pending; T03.4 remains queued.
