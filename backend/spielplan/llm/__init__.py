"""The LLM connector layer -- §8 stage 6, the one paid stage. Spec v2.1 §9, §8, §6.6.

§9 is the whole brief for this package: "Ported design (no vendor SDKs; one POST per provider
through the rate-limited fetcher; batch endpoints supported) ... including the reasoning-token
accounting corrections (Gemini bills thinking tokens as output - counting visible JSON understates
cost ~5x) ... The schema is a cost-saving device, not the guarantee - the guarantee is the
validator (ported principle). Two-attempt pattern: retry once with the specific contract violation
named." Decision 338 narrows the first sentence for M5: batch endpoints do not ship.

What lives here, one module per job, and not one of them is a verdict on what a model said:

  * `client` -- the shape the three adapters share (`LLMResult`, `LLMError`, the timeout, the one
    tool name, the fence-tolerant parse), the one call a caller makes, the free models-list probe
    §6.6's test button reads, and the fetcher a caller outside a drain opens.
  * `anthropic`, `openai`, `gemini` -- the three structured-output mechanisms, forced tool-use,
    strict schema and `responseSchema`, each one POST through M5.1's fetcher with the key in the
    header its provider documents.
  * `contract` -- the ONE extraction schema the three project from, the prompt, and the retry
    message that names what M5.4's validator refused.
  * `pricing`, `spend`, `consensus`, `extract` -- the dated price table (decision 343), the meter
    and the cap (decision 325), the run arithmetic (decision 337) and stage 6 itself (decision
    432), each arriving with its own step of M5.5.

THE VALIDATOR IS NOT HERE, AND THAT IS A FACT ABOUT IMPORTS. M5.4's `dna/verify.py` is the trust
boundary, and `test_dna_verify.py::test_no_provider_client_is_imported_by_the_trust_boundary`
holds that it imports nothing from this package. The direction is one-way on purpose: stage 6
calls `verify_payload` with a decoded payload and gets a verdict back, and a validator one
convenience import away from the client it judges is a validator that can start trusting it. The
corpus keeps the same boundary the same way -- `mdc/dna/store.py` does not import
`mdc/llm/client.py` -- and `dna/__init__.py` states it from the other side. So nothing here
re-implements a check `verify_payload` makes: `contract` formats its verdicts and no more.

Nothing is re-exported from this file, for `dna/__init__.py`'s reason: a package `__init__` that
grows an `__all__` is where callers stop naming the module they mean, and a provider adapter, a
price table and a meter are separate jobs that a caller should have to name.
"""
