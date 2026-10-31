"""Kin words in English, French, Spanish, German, Russian and Japanese (v1.2.12).

Used in two ways, and only these two:
  * to find the sentences of a book that might state a family tie (a TRIGGER: the sentence is put to the model, which says
    how two named people are related, or "none");
  * to check that a tie the model reports is backed by a word of the matching kind in the sentence (parent/child words
    for father, mother, son, daughter; marriage words for husband and wife; and so on).
A word here never states a relation by itself.

Titles are not kin: "Father Nicanor", "Padre Antonio", "Sister Agnes" name a priest or a nun. A kin word followed by a
capitalised word counts only when it comes after a possessive or an article ("his father Gabriel", "Gabriel's son
Stephan", "la madre Ursula").

Extra words for a book: ~/.claude-xray/kin_words.txt, one "word relation" per line (relation: father, mother, son,
daughter, husband, wife, brother, sister, uncle, aunt, nephew, niece, cousin, grandfather, grandmother, grandson,
granddaughter, father-in-law ...), "#" starts a comment.
"""
import re
import unicodedata
from pathlib import Path

# group codes: PC parent/child, SP spouse, SB sibling, UN uncle/aunt/nephew/niece, CO cousin, GP grandparent/grandchild, IL in-law
RELATION_GROUP = {
    "father": "PC", "mother": "PC", "son": "PC", "daughter": "PC", "parent": "PC", "child": "PC",
    "husband": "SP", "wife": "SP", "brother": "SB", "sister": "SB", "sibling": "SB",
    "uncle": "UN", "aunt": "UN", "nephew": "UN", "niece": "UN", "cousin": "CO",
    "grandfather": "GP", "grandmother": "GP", "grandson": "GP", "granddaughter": "GP",
    "father-in-law": "IL", "mother-in-law": "IL", "son-in-law": "IL", "daughter-in-law": "IL",
    "brother-in-law": "IL", "sister-in-law": "IL",
}
# category names used by enrich._CATEGORY -> group code
CATEGORY_GROUP = {"spouse": "SP", "parent": "PC", "child": "PC", "sibling": "SB", "uncle": "UN", "nephew": "UN",
                  "cousin": "CO", "grandparent": "GP", "grandchild": "GP", "inlaw_parent": "IL", "inlaw_child": "IL",
                  "inlaw_sibling": "IL"}

_LATIN = {
    "PC": """father mother son daughter parent parents child children fathers mothers sons daughters papa mama mommy daddy mum dad
             pere mere fils fille filles enfant enfants maman papa papas
             padre madre hijo hija hijos hijas mijo mija mijito mijita papi mami padres
             vater mutter sohn tochter kind kinder eltern mutti vati papi mami sohne tochter
             otets otec mat mater syn doch dochka synok sinok batya matushka rebenok deti
             otosan okasan otousan okaasan chichi haha musuko musume otou okaa kodomo""",
    "SP": """husband wife husbands wives married marry marriage spouse widow widower wed wedding bride bridegroom groom
             mari femme epoux epouse veuve veuf mariee
             esposo esposa marido casado casada viuda viudo
             ehemann ehefrau gatte gattin verheiratet witwe witwer
             muzh zhena suprug vdova vdovets zhenat zamuzhem
             otto tsuma fufu okusan danna""",
    "SB": """brother sister siblings sibling twin twins brothers sisters
             frere soeur jumeau jumelle freres soeurs
             hermano hermana hermanos hermanas gemelo gemela
             bruder schwester geschwister zwilling bruders schwestern
             brat sestra bratya brata sestry bliznets
             ani ane otouto imouto oniisan oneesan oniichan oneechan niisan neesan kyodai shimai""",
    "UN": """uncle aunt nephew niece uncles aunts nephews nieces
             oncle tante neveu niece nieces
             tio tia sobrino sobrina tios tias
             onkel tante neffe nichte
             dyadya tetya plemyannik plemyannitsa
             oji oba ojisan obasan""",
    "CO": """cousin cousins cousine
             primo prima primos
             vetter base
             itoko""",
    "GP": """grandfather grandmother grandson granddaughter grandparents grandparent grandchildren grandchild grandpa grandma
             grandfathers grandmothers
             grandpere grandmere petitfils petitefille grandsparents
             abuelo abuela abuelos nieto nieta nietos
             grossvater grossmutter opa oma enkel enkelin grosseltern grossvater
             dedushka babushka ded vnuk vnuchka
             ojiisan obaasan sofu sobo mago""",
    "IL": """fatherinlaw motherinlaw soninlaw daughterinlaw brotherinlaw sisterinlaw inlaw inlaws
             beaupere bellemere gendre bru beaufrere bellesoeur
             suegro suegra yerno nuera cunado cunada
             schwiegervater schwiegermutter schwiegersohn schwiegertochter schwager schwagerin
             svekor svekrov test tyoshcha zyat nevestka""",
}
_CYR = {
    "PC": "отец мать мама папа сын дочь дочка сынок батя батюшка матушка дети ребенок родители",
    "SP": "муж жена супруг супруга вдова вдовец женат замужем",
    "SB": "брат сестра братья сестры близнец",
    "UN": "дядя тетя племянник племянница",
    "CO": "двоюродный двоюродная",
    "GP": "дедушка бабушка дед внук внучка",
    "IL": "свекор свекровь тесть теща зять невестка",
}
_CJK = {   # no spaces in Japanese: matched as substrings (a trigger only, so a compound that contains one is harmless)
    "PC": "父 母 息子 娘 お父さん お母さん 父親 母親 パパ ママ 子供",
    "SP": "夫 妻 夫婦 旦那 奥さん 嫁",
    "SB": "兄 姉 弟 妹 兄弟 姉妹 お兄さん お姉さん",
    "UN": "叔父 叔母 伯父 伯母 おじさん おばさん 甥 姪",
    "CO": "いとこ 従兄弟 従姉妹",
    "GP": "祖父 祖母 孫 おじいさん おばあさん",
    "IL": "義父 義母 義兄 義姉 義弟 義妹",
}

POSSESSIVES = set("""his her their my our your its whose the a an this that these those one
    su sus mi mis tu tus nuestro nuestra el la los las un una
    son sa ses mon ma mes ton ta tes leur leurs notre votre le les une
    sein seine seiner seinem seinen ihr ihre ihrer mein meine dein deine unser unsere der die das ein eine
    ego ee ih moy moya tvoy tvoya""".split())

_WORD = re.compile(r"[^\W\d_]+(?:[-'’][^\W\d_]+)*")
_extras_mtime = None
WORDS = {}       # group code -> set of folded single words
CJK = {}         # group code -> list of strings


def fold(s: str) -> str:
    """lower-case, accents removed, German eszett spelled out, one apostrophe: 'mamá' -> 'mama', 'Großvater' -> 'grossvater'"""
    s = s.replace("ß", "ss").replace("’", "'").lower()
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def _build(extras=None):
    WORDS.clear()
    CJK.clear()
    for g, txt in _LATIN.items():
        WORDS[g] = {fold(w) for w in txt.split()}
    for g, txt in _CYR.items():
        WORDS[g] |= {fold(w) for w in txt.split()}
    for g, txt in _CJK.items():
        CJK[g] = txt.split()
    for word, rel in (extras or []):
        g = RELATION_GROUP.get(rel)
        if g:
            if re.search(r"[぀-ヿ一-鿿]", word):
                CJK[g].append(word)
            else:
                WORDS[g].add(fold(word).replace("-", ""))


def load_extras(path: Path | None = None) -> list:
    path = path or Path.home() / ".claude-xray" / "kin_words.txt"
    out = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            parts = line.split()
            if len(parts) >= 2 and parts[1].lower() in RELATION_GROUP:
                out.append((parts[0], parts[1].lower()))
    except OSError:
        pass
    return out


def reload(path: Path | None = None):
    _build(load_extras(path))


reload()


def _lookup(token: str) -> str | None:
    t = fold(token).replace("-", "")
    for g, ws in WORDS.items():
        if t in ws:
            return g
    return None


def groups_in(text: str) -> set:
    """every kind of kin word the text contains (no title rule): used to check that a tie is backed by a word of its kind"""
    out = set()
    for m in _WORD.finditer(text):
        g = _lookup(m.group(0))
        if g:
            out.add(g)
    for g, ws in CJK.items():
        if any(w in text for w in ws):
            out.add(g)
    return out


def triggers(text: str) -> set:
    """the kinds of kin word the text uses as a relationship, not as a title: 'Father Nicanor' is a priest, 'his father
    Gabriel' and 'Gabriel's son Stephan' are tie words"""
    toks = [(m.group(0), m.start(), m.end()) for m in _WORD.finditer(text)]
    out = set()
    for k, (tok, a, b) in enumerate(toks):
        g = _lookup(tok)
        if not g:
            continue
        nxt = toks[k + 1][0] if k + 1 < len(toks) else ""
        title_like = bool(nxt) and nxt[0].isupper() and text[b:toks[k + 1][1]].strip() == ""
        if title_like:
            prev = toks[k - 1][0] if k else ""
            if not (fold(prev) in POSSESSIVES or prev.lower().endswith(("'s", "’s"))):
                continue
        out.add(g)
    for g, ws in CJK.items():
        if any(w in text for w in ws):
            out.add(g)
    return out


def has_trigger(text: str) -> bool:
    return bool(triggers(text))


def supports(group: str, text: str) -> bool:
    return group in groups_in(text)
