import re
import time
from pathlib import Path

import requests

CACHE_PATH = Path(__file__).parent / "tickers_cache.txt"
CACHE_MAX_AGE_SECONDS = 7 * 24 * 60 * 60  # refresh weekly, symbol lists change slowly

NASDAQ_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"

CASHTAG_RE = re.compile(r"\$([A-Za-z]{1,5})\b")
BAREWORD_RE = re.compile(r"\b([A-Z]{2,5})\b")

# Curated major cryptocurrencies. CoinGecko's full symbol list is too noisy for
# bareword matching (thousands of junk/meme tokens share symbols with common words).
CRYPTO_SYMBOLS = {
    "BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "BNB", "USDT", "USDC", "MATIC",
    "DOT", "LTC", "AVAX", "LINK", "ATOM", "XLM", "TRX", "SHIB", "PEPE", "WIF",
    "BONK", "TON", "NEAR", "ICP", "FIL", "APT", "ARB", "OP", "SUI", "INJ",
    "RNDR", "HBAR", "VET", "ALGO", "EGLD", "XMR", "ETC", "BCH", "FTM", "SAND",
    "MANA", "AXS", "GRT", "AAVE", "MKR", "CRV", "SNX", "COMP", "UNI", "RUNE",
    "KAS", "SEI", "TIA", "JUP", "PYTH", "STRK", "ONDO", "ENA",
}

# Common WSB slang / general English acronyms that collide with real ticker
# symbols and would otherwise flood bareword results with noise.
STOPWORDS = {
    "DD", "YOLO", "FOMO", "IMO", "IMHO", "LOL", "LMAO", "WTF", "TLDR", "CEO",
    "CFO", "CTO", "IPO", "ATH", "ATL", "ITM", "OTM", "EOD", "EOW", "PSA",
    "FYI", "ASAP", "RIP", "USA", "USD", "EUR", "GDP", "CPI", "FED", "SEC",
    "ETF", "API", "PDT", "EST", "PST", "AM", "PM", "OK", "GO", "ON", "IT",
    "SO", "ALL", "ARE", "FOR", "NOW", "NEW", "ONE", "TWO", "WHO", "WHY",
    "MAN", "CAN", "SEE", "BIG", "BUY", "SELL", "HOLD", "PUMP", "DUMP", "MOON",
    "BAG", "BAGS", "APE", "APES", "GUH", "WSB", "ETC", "INC", "LLC", "CORP",
    "US", "UK", "EU", "AI", "ML", "NFT", "GPU", "CPU", "PC", "TV", "OS",
    "NOT", "BUT", "HAS", "HAD", "HIS", "HER", "OUR", "OUT", "TOP", "ANY",
    "GET", "PUT", "GOT", "YET", "TOO", "ALSO", "EVEN", "JUST", "LIKE",
    "WILL", "WELL", "GOOD", "BAD", "REAL", "TRUE", "FAKE", "EASY", "HARD",
    "GAIN", "LOSS", "LOSE", "WIN", "RED", "GREEN", "YOU", "HERE", "THERE",
    "THIS", "THAT", "WHAT", "WHEN", "WHERE", "WITH", "FROM", "INTO", "OVER",
    "THAN", "THEN", "THEY", "THEM", "THEIR", "WERE", "WOULD", "COULD",
    "SHOULD", "ABOUT", "AFTER", "BEEN", "BEING", "MORE", "MOST", "SOME",
    "SUCH", "VERY", "JUST", "DOWN", "MUCH", "MANY", "STILL", "EVERY",
    "MAKE", "MADE", "TAKE", "TOOK", "COME", "CAME", "LOOK", "LOOKS",
    "WANT", "NEED", "KNOW", "THINK", "GUYS", "GUY", "SIR", "PPI", "CPI",
    "JPY", "USD", "EUR", "GBP", "CAD", "AUD", "CHF", "NZD", "ES", "NQ",
    "SPX", "VIX", "KEEL", "HOLY", "SHIT", "FUCK", "DAMN", "OMG",
    "TBH", "NGL", "AF", "IRL", "OP", "TIL", "PS", "BTW", "IDK", "IDC",
}


def _download_symbols(url: str, symbol_column: int) -> set[str]:
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    symbols = set()
    for line in response.text.splitlines()[1:]:  # skip header
        parts = line.split("|")
        if len(parts) > symbol_column:
            symbol = parts[symbol_column].strip()
            if symbol and symbol.isalpha():
                symbols.add(symbol.upper())
    return symbols


def _refresh_cache() -> set[str]:
    nasdaq_symbols = _download_symbols(NASDAQ_URL, symbol_column=0)
    other_symbols = _download_symbols(OTHER_URL, symbol_column=0)
    all_symbols = nasdaq_symbols | other_symbols
    CACHE_PATH.write_text("\n".join(sorted(all_symbols)), encoding="utf-8")
    return all_symbols


def load_known_stock_symbols() -> set[str]:
    if CACHE_PATH.exists() and (time.time() - CACHE_PATH.stat().st_mtime) < CACHE_MAX_AGE_SECONDS:
        return set(CACHE_PATH.read_text(encoding="utf-8").splitlines())

    try:
        return _refresh_cache()
    except requests.RequestException:
        if CACHE_PATH.exists():
            return set(CACHE_PATH.read_text(encoding="utf-8").splitlines())
        return set()


def extract_symbols(text: str, known_stock_symbols: set[str]) -> list[tuple[str, str]]:
    """Returns a list of (symbol, confidence) tuples found in the text."""
    matches: dict[str, str] = {}

    for raw in CASHTAG_RE.findall(text):
        matches[raw.upper()] = "cashtag"

    for raw in BAREWORD_RE.findall(text):
        symbol = raw.upper()
        if symbol in matches or symbol in STOPWORDS:
            continue
        if symbol in known_stock_symbols or symbol in CRYPTO_SYMBOLS:
            matches[symbol] = "bareword"

    return list(matches.items())
