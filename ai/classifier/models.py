"""Pydantic request/response models for the classifier API.

These models are used both for request validation (FastAPI deserialises
incoming JSON into them) and for type safety throughout the analysis pipeline.

Data model relationships
------------------------
  POST /assess
    image (UploadFile)  +  criteria (list[CriterionInput])
        → queued as an assess job → analyzed by analysis.py

  POST /assess/compare
    CompareRequest
      ├── image     : ImageInput          — the subject image
      ├── criteria  : list[CriterionInput]— what to evaluate
      ├── aggregation: mean|min|max       — how to collapse N example scores
      └── examples  : list[ExampleInput]  — reference images to compare against

Process flow position: imported by analysis.py, main.py, workers.py, and llm.py.
"""

from typing import Literal, Optional

from pydantic import BaseModel, Field

from config import DEFAULT_CRITERIA


class CriterionInput(BaseModel):
    """A single evaluation criterion.

    Two types are supported:
      - "quality": the LLM scores image quality on a 1-10 scale.
      - "feature": the LLM detects whether a specific feature is present
                   (10 = clearly present, 5 = uncertain, 1 = absent).

    Weights control how much each criterion contributes to the overall score.
    A criterion with weight=3.0 counts three times as heavily as one with
    weight=1.0 in the weighted average computed by validate_and_clamp().
    """

    name: str = Field(description="The criterion to evaluate.")
    type: Literal["cv", "llm"] = Field(
        default="llm",
        description=(
            "'llm': scored by the vision LLM (no detector required). "
            "'cv': run through a registered OpenCV detector by name — no token cost. "
            "If no detector matches the criterion name, falls back to 'llm' automatically. "
            "The result always includes a 'method' field showing which path was actually used. "
            "Built-in cv names: 'sharpness', 'exposure' / 'proper exposure', "
            "'has trees' / 'has vegetation' / 'has greenery' / 'has plants', "
            "'has sky', 'has faces' / 'has people' / 'has person', "
            "'has water' / 'has pool' / 'has swimming pool', "
            "'has text' / 'has text regions' / 'has writing'."
        ),
    )
    weight: float = Field(
        default=1.0,
        gt=0.0,
        description=(
            "Relative weight for this criterion when computing the overall score. "
            "Higher values make this criterion matter more. Default 1.0 for equal weighting."
        ),
    )
    hint: Literal["quality", "presence", "auto"] = Field(
        default="auto",
        description=(
            "Tells the LLM which scoring rubric to apply to this criterion. "
            "'quality': score image quality 1-10 (1-3=FAIL, 4-6=MARGINAL, 7-10=PASS). "
            "'presence': detect presence/absence (10=present, 5=uncertain, 1=absent) with "
            "chain-of-thought reasoning. "
            "'auto' (default): LLM infers the rubric from the criterion name. "
            "For type='cv' criteria: hint is ignored when a detector runs, but is passed "
            "to the LLM if no detector matches the name and the criterion falls back."
        ),
    )
    depends_on: Optional[str] = Field(
        default=None,
        description=(
            "Name of another criterion that must PASS (score >= 7) before this "
            "criterion is evaluated. If the dependency does not pass — including "
            "if it was itself skipped — this criterion is marked SKIPPED and "
            "excluded from the weighted score entirely. "
            "Chains are supported: A → B → C all skip if A fails. "
            "Future: a minimum score threshold will be configurable here; "
            "for now PASS verdict is the only qualifying condition."
        ),
    )


class ImageInput(BaseModel):
    """An image supplied as either a base64 string or a remote URL.

    Used in the JSON body of POST /assess/compare.  The 'type' field tells
    the loader which decoding path to take in analysis._load_bgr_from_input().
    URLs are SSRF-checked before fetching (see ssrf.py).
    """

    data: str = Field(description="Base64-encoded image string or a URL.")
    type: Literal["base64", "url"] = Field(description="Whether data is 'base64' or 'url'.")


class ExampleInput(BaseModel):
    """A reference image to compare the subject against in /assess/compare.

    Each example carries its own weight (how much similarity to this example
    influences the combined score) and optionally a pre-generated analysis
    (to skip the LLM call for a reference image that was already assessed).
    """

    data: str = Field(description="Base64-encoded image string or a URL.")
    type: Literal["base64", "url"] = Field(description="Whether data is 'base64' or 'url'.")
    weight: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description=(
            "How much similarity to this example influences the combined score (0.0–1.0). "
            "0.0 = ignore example entirely; 1.0 = only similarity matters."
        ),
    )
    pre_generated_analysis: Optional[dict] = Field(
        default=None,
        description=(
            "A prior analysis result for this example image. "
            "Provide this to skip the LLM call and save tokens — "
            "the value should be the 'example_analysis' dict from a previous response."
        ),
    )


class CompareRequest(BaseModel):
    """Full request body for POST /assess/compare.

    Compares a subject image against one or more reference examples,
    producing per-example similarity scores and an aggregate verdict.
    """

    image: ImageInput
    criteria: list[CriterionInput] = Field(
        default=DEFAULT_CRITERIA,
        description="List of criteria objects, each with a name and type ('quality' or 'feature').",
    )
    aggregation: Literal["mean", "min", "max"] = Field(
        default="mean",
        description=(
            "How to collapse per-example combined scores into a single aggregate verdict. "
            "mean = balanced; min = strictest (must match all examples); "
            "max = most lenient (must match any example)."
        ),
    )
    examples: list[ExampleInput] = Field(
        min_length=1,
        description="One or more reference images to compare the subject against.",
    )
