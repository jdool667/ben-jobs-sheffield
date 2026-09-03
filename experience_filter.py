"""Extract required years of experience from a job description (stdlib only).

Moved out of linkedin-jobs-scraper.py so build_jobs_site.py can reuse the exact
same extractor without pulling in pandas/jobspy. Behaviour is identical.
"""
import re

# Spelled-out numbers we accept in "five years' experience" style phrasing.
_WORD_NUMBERS = {
    'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
    'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10,
}
# A year-count token: a digit run OR a spelled-out number one..ten.
_NUM = r'(\d+|one|two|three|four|five|six|seven|eight|nine|ten)'
_YEARS = r'(?:years?|yrs?)'
# Phrases that mark a year-count as "nice to have", not a hard requirement.
_PREFERRED = r'(?:a\s+)?(?:plus|bonus|nice[- ]to[- ]have|desirable|advantageous|preferred|beneficial|welcome|ideal|would be)'
# Strong experience signals: if any appears anywhere in the short window after a
# year-count, it's a work-experience figure (not "5 years ago" or "10 year visa").
_EXP_ANCHOR = r'experience|\bexp\b|commercial|professional|industry|hands[- ]on'


def _to_year_number(token):
    """Convert a digit string or spelled-out number to an int (or None)."""
    token = token.strip()
    if token.isdigit():
        return int(token)
    return _WORD_NUMBERS.get(token)


def extract_years_experience(text):
    """
    Extract the required years of experience from a job description.
    Returns the HIGHEST *required* years figure, or None if none is stated.

    Aggregation policy (chosen for an early-career profile, max 2 years):
      - Multiple figures -> take the MAX of genuine requirements, so a senior
        role isn't waved through just because it also lists a low tech-specific
        number (e.g. "5 years overall, 2 years with React" -> 5, not 2).
      - Ranges ("5-7 years") use the LOWER bound (the floor they'll accept).
      - Figures in "plus / bonus / nice-to-have / preferred" contexts are
        ignored - they're not hard requirements.
    Handles possessive apostrophes ("5 years' experience") and spelled-out
    numbers ("five years"), both of which the old digit-only regexes missed.
    """
    # NaN-safe guard (pandas may pass a float nan; nan != nan is True).
    if text is None or (isinstance(text, float) and text != text) or not text:
        return None

    text = str(text).lower()
    # FIX: descriptions are markdown, so punctuation is backslash-escaped
    # ("10\-15yrs", "0\-2 years", "full\-stack"). Unescape so ranges and the
    # entry-level "0-2 years" rule actually match.
    text = re.sub(r'\\([-.()#+])', r'\1', text)
    # FIX: normalise possessive apostrophes so "years'"/"years’" parse as "years".
    text = re.sub(r"(" + _YEARS + r")['’]", r"\1", text)

    # Explicit entry-level signals -> treat as new-grad friendly (0 years),
    # but ignore them when negated ("this is NOT an entry-level role").
    entry_level_indicators = [
        r'entry[- ]level',
        r'fresh grad(?:uate)?',
        r'recent grad(?:uate)?',
        r'new grad(?:uate)?',
        r'no experience required',
        r'no prior experience',
        r'0[- ]?(?:to|-)?\s*[12]\s*years?',  # 0-1, 0-2 years
        r'less than\s+[12]\s+years?',
        r'up to\s+[12]\s+years?'
    ]
    for pattern in entry_level_indicators:
        m = re.search(pattern, text)
        if m and not re.search(r'\b(?:not|beyond|more than|above)\b',
                               text[max(0, m.start() - 14):m.start()]):
            return 0

    # Vague seniority cues with no number -> assume senior (>= 3 years).
    vague_senior_patterns = [
        r'several years?.{0,20}(?:of\s+)?(?:experience|exp)',
        r'many years?.{0,20}(?:of\s+)?(?:experience|exp)',
        r'multiple years?.{0,20}(?:of\s+)?(?:experience|exp)',
        r'extensive\s+(?:experience|exp)',
        r'substantial\s+(?:experience|exp)',
        r'significant\s+(?:experience|exp)',
        r'proven\s+track\s+record',
        r'demonstrated\s+(?:experience|exp)',
    ]
    for pattern in vague_senior_patterns:
        if re.search(pattern, text):
            return 3

    required = []  # collect every genuine year requirement; we take the max

    # Ranges first ("5-7 years", "5 to 7 years"): use the lower bound, and blank
    # the span out so the single-figure pass below doesn't double-count "7 years".
    range_re = re.compile(_NUM + r'\s*(?:[-–]|to)\s*' + _NUM + r'\s*\+?\s*' + _YEARS)
    masked = text
    for m in range_re.finditer(text):
        lo, hi = _to_year_number(m.group(1)), _to_year_number(m.group(2))
        masked = masked[:m.start()] + ' ' * (m.end() - m.start()) + masked[m.end():]
        if lo is None or hi is None:
            continue
        if re.search(_PREFERRED, text[m.end():m.end() + 45]):
            continue
        required.append(min(lo, hi))

    # Single figures ("5+ years", "minimum 3 years", "five years' experience").
    single_re = re.compile(_NUM + r'\s*\+?\s*' + _YEARS)
    for m in single_re.finditer(masked):
        n = _to_year_number(m.group(1))
        if n is None:
            continue
        leading = masked[max(0, m.start() - 25):m.start()]
        trailing = masked[m.end():m.end() + 50]
        # Must look like an experience requirement, not an incidental year count.
        # "in"/"with" only count when they directly follow the years ("5 years in
        # Java"), so company-growth copy ("16 years ... doubling, now in profit")
        # doesn't get mistaken for a requirement.
        if not (re.search(_EXP_ANCHOR, trailing)
                or re.match(r'\s*(?:in|with)\b', trailing)
                or re.search(r'(?:minimum|at least|least|require|need)', leading)):
            continue
        if re.search(_PREFERRED, trailing):
            continue
        required.append(n)

    return max(required) if required else None
