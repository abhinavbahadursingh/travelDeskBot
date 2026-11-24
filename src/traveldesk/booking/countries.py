"""ISO 3166-1 alpha-2 country codes, for passport and nationality fields.

IATA passenger data (API/PNR) and ICAO Doc 9303 passports identify the
issuing state and nationality by country code; LetsFG takes the 2-letter
ISO form. A code that matches the pattern but names no country (e.g. "UK",
"XX") is a typo that the airline would reject at check-in.
"""

from __future__ import annotations

ISO_ALPHA2 = frozenset(
    """
AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ
BR BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM
DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS
GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN
KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ
MR MS MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM
PN PR PS PT PW PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV
SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI
VN VU WF WS YE YT ZA ZM ZW
""".split()
)

# Common non-ISO spellings → the ISO code to suggest.
SUGGEST = {"UK": "GB", "EN": "GB"}

# Passports legitimately issued by a territory other than the holder's
# nationality: (nationality, issuing country). Chinese nationals hold Hong
# Kong SAR or Macao SAR passports.
NATIONALITY_ISSUER_EXCEPTIONS = frozenset({("CN", "HK"), ("CN", "MO")})


def is_country(code: str) -> bool:
    return code in ISO_ALPHA2


def issuer_matches(nationality: str, issuer: str) -> bool:
    return nationality == issuer or (nationality, issuer) in NATIONALITY_ISSUER_EXCEPTIONS
