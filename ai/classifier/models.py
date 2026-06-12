from typing import Literal, Optional

from pydantic import BaseModel, Field

from config import DEFAULT_CRITERIA


class CriterionInput(BaseModel):
    name: str = Field(description="The criterion to evaluate.")
    type: Literal["quality", "feature"] = Field(
        default="quality",
        description=(
            "'quality': score image quality 1-10 (1-3=FAIL, 4-6=MARGINAL, 7-10=PASS). "
            "'feature': detect presence/absence (10=clearly present, 5=uncertain, 1=clearly absent)."
        ),
    )


class ImageInput(BaseModel):
    data: str = Field(description="Base64-encoded image string or a URL.")
    type: Literal["base64", "url"] = Field(description="Whether data is 'base64' or 'url'.")


class ExampleInput(BaseModel):
    data: str = Field(description="Base64-encoded image string or a URL.")
    type: Literal["base64", "url"] = Field(description="Whether data is 'base64' or 'url'.")
    weight: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="How much similarity to this example influences the combined score (0.0–1.0).",
    )
    pre_generated_analysis: Optional[dict] = Field(
        default=None,
        description="A prior analysis result for this example. Providing this skips the LLM call.",
    )


class CompareRequest(BaseModel):
    image: ImageInput
    criteria: list[CriterionInput] = Field(
        default=DEFAULT_CRITERIA,
        description="List of criteria objects, each with a name and type ('quality' or 'feature').",
    )
    aggregation: Literal["mean", "min", "max"] = Field(
        default="mean",
        description="How to collapse per-example combined scores into a single aggregate verdict.",
    )
    examples: list[ExampleInput] = Field(
        min_length=1,
        description="One or more reference examples to compare the input against.",
    )
