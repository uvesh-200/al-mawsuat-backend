import asyncio

from fastapi import FastAPI
from langdetect import detect
from pydantic import BaseModel, Field
from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

app = FastAPI(title="Translation Server")

MODEL_NAME = "facebook/nllb-200-distilled-600M"
tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
model = AutoModelForSeq2SeqLM.from_pretrained(MODEL_NAME)

NLLB_CODES = {
    "ar": "arb_Arab",
    "ur": "urd_Arab",
    "en": "eng_Latn",
}


class DetectRequest(BaseModel):
    text: str = Field(min_length=1)


class DetectResponse(BaseModel):
    language: str


class TranslateRequest(BaseModel):
    text: str = Field(min_length=1)
    source_lang: str = Field(min_length=2)
    target_lang: str = Field(min_length=2)


class TranslateResponse(BaseModel):
    translated: str


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "model": MODEL_NAME}


@app.post("/detect", response_model=DetectResponse)
async def detect_language(req: DetectRequest) -> DetectResponse:
    lang = detect(req.text)
    if lang not in NLLB_CODES:
        lang = "en"
    return DetectResponse(language=lang)


def _translate(text: str, source_lang: str, target_lang: str) -> str:
    src = NLLB_CODES.get(source_lang, source_lang)
    tgt = NLLB_CODES.get(target_lang, target_lang)
    tokenizer.src_lang = src
    inputs = tokenizer(text, return_tensors="pt", padding=True, truncation=True)
    outputs = model.generate(
        **inputs,
        forced_bos_token_id=tokenizer.convert_tokens_to_ids(tgt),
        max_length=512,
    )
    return tokenizer.decode(outputs[0], skip_special_tokens=True)


@app.post("/translate", response_model=TranslateResponse)
async def translate(req: TranslateRequest) -> TranslateResponse:
    result = await asyncio.to_thread(_translate, req.text, req.source_lang, req.target_lang)
    return TranslateResponse(translated=result)
