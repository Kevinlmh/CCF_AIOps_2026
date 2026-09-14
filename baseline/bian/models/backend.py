"""Small local Transformers backend shared by the two logical BiAn roles."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Callable, Mapping

from .structured_output import parse_and_validate


@dataclass(frozen=True)
class ModelConfig:
    max_input_tokens: int = 8192
    batch_size: int = 8
    device_max_new_tokens: int = 256
    stage1_max_new_tokens: int = 256
    stage2_max_new_tokens: int = 384
    classification_max_new_tokens: int = 192
    retries: int = 1
    seed: int = 42

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ModelConfig":
        """Separate model-loading options from pipeline orchestration options."""
        model_fields = set(cls.__dataclass_fields__)
        orchestration_fields = {"llm_candidate_limit"}
        unknown = set(value) - model_fields - orchestration_fields
        if unknown:
            names = ", ".join(sorted(unknown))
            raise ValueError(f"unknown local model configuration fields: {names}")
        return cls(**{key: value[key] for key in model_fields if key in value})


EXPECTED_KEYS = {
    "7b_a_device_analysis": "device_analyses",
    "7b_b_stage1": "scores",
    "7b_b_stage2": "candidates",
    "classification": None,
}

RESPONSE_PREFIXES = {
    "7b_b_stage2": '{"candidates":[',
}


def model_load_options(*, cuda_available: bool) -> dict[str, Any]:
    """Return device placement options without importing Torch."""
    return {"device_map": "auto"} if cuda_available else {}


def response_prefix_for(prompt_name: str) -> str:
    """Schema-only assistant prefill; contains no candidate or label values."""
    return RESPONSE_PREFIXES.get(prompt_name, "")


class JsonModelBackend:
    def __init__(self, model: str, config: ModelConfig, prompt_dir: Path):
        self.model = model
        self.config = config
        self.prompt_dir = prompt_dir
        self._tokenizer = None
        self._network = None

    def load(self) -> None:
        if self._network is not None:
            return
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
        local = Path(self.model).exists()
        self._tokenizer = AutoTokenizer.from_pretrained(self.model, local_files_only=local, trust_remote_code=False)
        kwargs = {"trust_remote_code": False, "local_files_only": local, "low_cpu_mem_usage": True}
        if torch.cuda.is_available():
            kwargs.update({"torch_dtype": torch.bfloat16})
        kwargs.update(model_load_options(cuda_available=torch.cuda.is_available()))
        self._network = AutoModelForCausalLM.from_pretrained(self.model, **kwargs)
        self._network.eval()

    def _render_prompt(self, name: str, payload: dict[str, Any]) -> str:
        template = (self.prompt_dir / f"{name}.txt").read_text(encoding="utf-8")
        return template + "\n\nINPUT_JSON:\n" + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def generate_json(self, *, role: str, prompt_name: str, payload: dict[str, Any], validator: Callable[[Any], dict[str, Any]], max_new_tokens: int) -> dict[str, Any]:
        try:
            self.load()
        except Exception as exc:
            reason = " ".join(str(exc).splitlines()).replace(self.model, "<model>")[:500]
            raise RuntimeError(
                f"{role}/{prompt_name} model loading failed: "
                f"{type(exc).__name__}: {reason}"
            ) from None
        import torch
        assert self._tokenizer is not None and self._network is not None
        error = None
        for attempt in range(1, self.config.retries + 2):
            prompt = self._render_prompt(prompt_name, payload)
            rendered = self._tokenizer.apply_chat_template([{"role": "user", "content": prompt}], tokenize=False, add_generation_prompt=True)
            rendered += "<think>\n</think>\n"
            response_prefix = response_prefix_for(prompt_name)
            rendered += response_prefix
            inputs = self._tokenizer(rendered, return_tensors="pt", truncation=True, max_length=self.config.max_input_tokens)
            device = next(self._network.parameters()).device
            inputs = {key: value.to(device) for key, value in inputs.items()}
            with torch.inference_mode():
                output = self._network.generate(**inputs, max_new_tokens=max_new_tokens, do_sample=False, pad_token_id=self._tokenizer.eos_token_id, use_cache=True)
            generated = output[0, inputs["input_ids"].shape[-1]:]
            raw = response_prefix + self._tokenizer.decode(generated, skip_special_tokens=True)
            try:
                return parse_and_validate(
                    raw,
                    expected_key=EXPECTED_KEYS.get(prompt_name),
                    validator=validator,
                )
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
        raise ValueError(f"{role}/{prompt_name} failed after retries: {error}")
