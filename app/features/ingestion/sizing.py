"""Shared chunk-sizing and sentence-boundary constants."""
import re

SENTENCE_END = re.compile(r"[.۔!?؟]$")
HADITH_PATTERN = re.compile(r"(?:حديث\s*رقم|باب\s+\d+|رقم\s*\d+)")
AYAH_OPEN = "\ufefd"
AYAH_CLOSE = "\ufefe"
CHAPTER_WORDS = ("باب", "كتاب", "الفصل")

HARD_CEILING = 900
TARGET_MAX = 420  # soft upper bound; complete paragraphs grouped into ~420-word chunks
MIN_TARGET = 200  # don't close a chunk below this size unless it's the tail
OVERLAP_FRACTION = 0.15  # next chunk re-includes ~15% of the previous chunk's tail
CROSS_PAGE_MERGE_THRESHOLD = 150  # merge consecutive chunks if both are this short
