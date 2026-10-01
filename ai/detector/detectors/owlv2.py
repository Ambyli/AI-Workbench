"""OWL family — OWLv2 and OWL-ViT, via ``transformers``.

Google's OWL models take an image plus a list of free-text queries and return
a box per query per detection. No fine-tuning, no fixed class list: "swimming
pool" and "roof vent" work as well as "car" does, which is the whole reason
this service exists.

Two things about OWLv2 are easy to get wrong and are handled here:

  **1. The processor pads to a square.** ``Owlv2ImageProcessor`` pads the
  image on the bottom and right to a square before resizing to the model's
  960-px grid. If ``target_sizes`` is the *unpadded* image size, every box
  comes back stretched — x by ``W/max(W,H)``, y by ``H/max(W,H)`` — which on
  a portrait photo means boxes that are systematically too wide and too tall.
  The fix is to post-process against the PADDED square and rely on the
  padding being bottom-right, so the top-left origin is shared and boxes only
  need clipping. :meth:`_detect_prepared` does that; the base class clips.

  **2. The post-processing method was renamed.** ``transformers`` 5.x exposes
  ``post_process_grounded_object_detection``; 4.x had
  ``post_process_object_detection`` (still present on some 4.x releases, gone
  in 5). The model card still shows the old name. We resolve the attribute at
  call time and use whichever exists, so this module survives either pin.

Process flow position: constructed by ``detectors.build_detector`` when the
model id names an OWL checkpoint.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from logger import logger

from .base import Detection, OpenVocabularyDetector

if TYPE_CHECKING:  # pragma: no cover - typing only
    from PIL.Image import Image


class Owlv2Detector(OpenVocabularyDetector):
    """OWLv2 / OWL-ViT through ``Owlv2Processor`` + ``Owlv2ForObjectDetection``.

    Both checkpoints load through the same auto classes, so one
    implementation covers the family; ``family`` reports which id it was
    built for rather than pretending they are the same model.
    """

    family = "owl"

    def __init__(self, model_id: str, device: str, *, max_image_side: int) -> None:
        super().__init__(model_id, device, max_image_side=max_image_side)
        self._processor = None
        self._model = None

    # ── Lifecycle ─────────────────────────────────────────────────────────
    def load(self) -> None:
        """Build the processor and model and move it to the device.

        Idempotent. ``AutoProcessor`` / ``AutoModelForZeroShotObjectDetection``
        rather than the concrete ``Owlv2*`` classes so an OWL-ViT id loads
        through the same path — the concrete classes are what the model card
        shows, but the auto classes read the checkpoint's own config, which is
        what makes ``DETECTOR_MODEL`` a real knob.
        """
        if self._loaded:
            return

        import torch
        from transformers import AutoModelForZeroShotObjectDetection, AutoProcessor

        logger.info(
            "Owlv2Detector.load: loading %s onto %s", self.model_id, self.device
        )
        self._processor = AutoProcessor.from_pretrained(self.model_id)
        model = AutoModelForZeroShotObjectDetection.from_pretrained(self.model_id)

        # fp16 on GPU halves the weights and the activations, which is what
        # makes this fit in the headroom qwen3.8-solo leaves on GPU 2. On CPU
        # it is a pessimisation — most CPU kernels upcast anyway — so the
        # weights stay fp32 there.
        if self.device == "cuda":
            model = model.half()
        model = model.to(self.device)
        model.eval()
        self._model = model
        self._loaded = True

        params = sum(p.numel() for p in model.parameters())
        logger.info(
            "Owlv2Detector.load: ready — %s, %.1fM parameters, dtype=%s, device=%s",
            self.model_id,
            params / 1e6,
            next(model.parameters()).dtype,
            self.device,
        )
        del torch  # imported only to make the failure mode obvious if missing

    # ── Inference ─────────────────────────────────────────────────────────
    def _detect_prepared(
        self, image: "Image", labels: list[str], threshold: float
    ) -> list[Detection]:
        """Run OWLv2 on the prepared image; boxes in PREPARED pixels.

        Steps:
          1. Encode the image and every label as one batch of text queries.
          2. Forward pass under ``inference_mode`` — no autograd graph, which
             is both faster and the difference between fitting in the GPU 2
             headroom and not.
          3. Post-process against the PADDED square (see the module docstring),
             with the caller's threshold so the model's own filter does the
             work.
          4. Map each result's label index back to the caller's exact string.
        """
        import torch

        width, height = image.size
        inputs = self._processor(
            text=[labels], images=image, return_tensors="pt"
        )
        inputs = {k: v.to(self.device) for k, v in inputs.items()}
        if self.device == "cuda":
            # Only the pixel tensor is floating point; input_ids / attention
            # masks are integer and must not be cast.
            for key, value in inputs.items():
                if value.dtype.is_floating_point:
                    inputs[key] = value.half()

        with torch.inference_mode():
            outputs = self._model(**inputs)

        # Step 3 — the padded square is what the boxes are normalised against.
        side = max(width, height)
        post = getattr(
            self._processor, "post_process_grounded_object_detection", None
        ) or getattr(self._processor, "post_process_object_detection")
        results = post(
            outputs=outputs,
            threshold=threshold,
            target_sizes=[(side, side)],
        )[0]

        boxes = results["boxes"].detach().float().cpu().tolist()
        scores = results["scores"].detach().float().cpu().tolist()
        indices = results["labels"].detach().cpu().tolist()

        detections: list[Detection] = []
        for box, score, index in zip(boxes, scores, indices):
            # Step 4 — the model answers with an index into the query list we
            # sent, so the caller's exact spelling comes back untouched.
            if not 0 <= index < len(labels):
                logger.warning(
                    "Owlv2Detector: label index %s out of range for %d labels",
                    index,
                    len(labels),
                )
                continue
            detections.append(
                Detection(label=labels[index], score=float(score), box=tuple(box))
            )

        logger.debug(
            "Owlv2Detector._detect_prepared: %dx%d, %d label(s), threshold=%.3f "
            "-> %d box(es)",
            width,
            height,
            len(labels),
            threshold,
            len(detections),
        )
        return detections
