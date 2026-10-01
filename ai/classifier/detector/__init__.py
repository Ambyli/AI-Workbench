"""The open-vocabulary detector service, as this service's client sees it.

One module, ``client``, wrapping the `ai/detector` container: POST an image
and a list of free-text labels, get boxes back. It is what lets an arbitrary
``has bicycle`` criterion localise — and be scored — without a vision-LLM
call.

Nothing is re-exported. Callers write ``from detector import client as
detector_client`` and reach the module itself, because ``is_configured()``
reads DETECTOR_URL at call time and a test points that at a stub by setting
the module attribute.

What the PIPELINE does with the boxes is ``analysis.detector_eval``; this
package is only the transport.
"""
