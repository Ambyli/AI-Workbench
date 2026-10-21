"""One module per model lane: how a text becomes features.

    pii.py        gravitee-io/bert-small-pii-detection -> entity types, counts, scores
    embed.py      intfloat/multilingual-e5-small -> query vectors, example sets, kNN
    injection.py  NeuralTrust/prompt-guard-oss-small -> P(jailbreak), head + tail windows
    language.py   fastText lid.176 -> language, confidence

Each model class has a ``load(root, spec, threads)`` constructor (runtime.py
calls it), a ``warm_up()`` that runs one inference on a synthetic sentence
and checks the output's shape, and a ``scan`` generator per text: it yields
between units of work (a token window, a batch) so a lane worker can stop
or hand the model to a waiting request there (scheduler.py), and returns the
text's features. The pure parts -- label mapping, span rules, kNN shares --
are module functions the unit tests run without a model.

Nothing here keeps or returns text, spans or offsets: spans live inside a
``scan`` call and leave it only as type counts.
"""
