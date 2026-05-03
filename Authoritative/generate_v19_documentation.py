"""Generate comprehensive v19 pipeline documentation as a Word document.

Output: v19_pipeline_documentation.docx
- Heading-based navigation (Word auto-TOC compatible)
- Speaker notes under each section
- Full articulated descriptions of every implementation detail
- No em dashes; defines key terms (whitelist, primaries, SOCs)
- Describes chunking-based source processing with regex extraction
"""
from docx import Document
from docx.shared import Pt, Inches, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
from pathlib import Path
import shutil


def add_toc(doc):
    """Insert a Word auto-TOC field that updates when the doc is opened."""
    p = doc.add_paragraph()
    run = p.add_run()
    fld_char1 = OxmlElement('w:fldChar')
    fld_char1.set(qn('w:fldCharType'), 'begin')
    instr_text = OxmlElement('w:instrText')
    instr_text.set(qn('xml:space'), 'preserve')
    instr_text.text = 'TOC \\o "1-3" \\h \\z \\u'
    fld_char2 = OxmlElement('w:fldChar')
    fld_char2.set(qn('w:fldCharType'), 'separate')
    fld_char3 = OxmlElement('w:fldChar')
    fld_char3.set(qn('w:fldCharType'), 'end')
    run._element.append(fld_char1)
    run._element.append(instr_text)
    run._element.append(fld_char2)
    run._element.append(fld_char3)
    placeholder = doc.add_paragraph(
        '(Right-click the table above and select "Update Field" in Word to populate the contents.)'
    )
    placeholder.runs[0].italic = True
    placeholder.runs[0].font.size = Pt(9)


def speaker_note(doc, text):
    p = doc.add_paragraph()
    p.paragraph_format.left_indent = Inches(0.3)
    p.paragraph_format.space_before = Pt(4)
    p.paragraph_format.space_after = Pt(8)
    run = p.add_run("Speaker note. ")
    run.bold = True
    run.italic = True
    run.font.color.rgb = RGBColor(0x55, 0x55, 0x55)
    run2 = p.add_run(text)
    run2.italic = True
    run2.font.color.rgb = RGBColor(0x55, 0x55, 0x55)


def body(doc, text):
    p = doc.add_paragraph(text)
    p.paragraph_format.space_after = Pt(6)


def bullet(doc, text):
    p = doc.add_paragraph(text, style='List Bullet')
    p.paragraph_format.space_after = Pt(2)


def main():
    doc = Document()

    # Title
    title = doc.add_heading('V19 Pipeline: End-to-End Documentation', level=0)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    sub = doc.add_paragraph()
    sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
    sub_run = sub.add_run('O*NET Multiple-Choice Question Benchmark Pipeline\n'
                           'Wage-Weighted 500-SOC Run with Academic Source Integration\n'
                           'May 1, 2026')
    sub_run.italic = True
    sub_run.font.size = Pt(11)

    doc.add_paragraph()

    # ───── TABLE OF CONTENTS ─────
    doc.add_heading('Table of Contents', level=1)
    add_toc(doc)
    doc.add_page_break()

    # ───── 1. EXECUTIVE SUMMARY ─────
    doc.add_heading('1. Executive Summary', level=1)
    body(doc,
        "The V19 pipeline produces an O*NET aligned multiple choice question (MCQ) benchmark by "
        "combining wage bill weighted occupation selection with multi source authoritative content "
        "discovery, nine sequential quality verifier passes with regenerate not reject paths, "
        "and a final replacement phase that backfills under yielded occupations with same rule "
        "wage bill ranked reserve occupations. The V19 run targeted 500 occupations drawn proportionally "
        "by wage bill share across the twenty three SOC major groups, with the new academic source "
        "integrations (PMC efetch, Crossref plus Unpaywall, arXiv) layered on top of the existing "
        "CDC, OSHA, BLS, and per occupation professional society and state licensing board "
        "whitelists.")
    speaker_note(doc,
        "When presenting V19, lead with the three architectural innovations: proportional "
        "wage bill allocation per plan section 3.2, regenerate not reject paths inside each "
        "verifier pass, and the academic source layer that broke the historical CDC dominance "
        "from 31 percent in v4 down to 16.5 percent in v19. Most reviewers will fixate on the "
        "source diversity figure first, so be ready to walk through the donut chart left to right.")

    # ───── 2. KEY TERMS / GLOSSARY ─────
    doc.add_heading('2. Key Terms and Glossary', level=1)
    body(doc,
        "This section defines the recurring vocabulary used throughout the rest of the document, "
        "so that later sections can refer to these terms without re-explaining them. Readers who "
        "are already familiar with the O*NET and BLS conventions can skip ahead to section 3.")

    doc.add_heading('2.1 SOC (Standard Occupational Classification)', level=2)
    body(doc,
        "SOC stands for Standard Occupational Classification, the United States federal taxonomy "
        "of jobs maintained by the Bureau of Labor Statistics. Each occupation has a six digit "
        "code (for example, 29-1141 for Registered Nurses or 15-1252 for Software Developers). "
        "The first two digits identify the SOC major group (29 for Healthcare Practitioners, 15 "
        "for Computer and Mathematical, and so on), and there are twenty three major groups in "
        "total. The pipeline uses the slightly extended O*NET-SOC variant which adds a two digit "
        "suffix after a period (for example, 29-1141.00). Throughout this document, references "
        "to a single SOC mean a single occupation entry in the O*NET-SOC taxonomy.")

    doc.add_heading('2.2 SOC Major Group', level=2)
    body(doc,
        "A SOC major group is a high level cluster of related occupations identified by the first "
        "two digits of the SOC code. The twenty three major groups range from group 11 (Management) "
        "and group 13 (Business and Financial Operations), through groups 29 (Healthcare Practitioners), "
        "47 (Construction and Extraction), 51 (Production), 53 (Transportation and Material Moving), "
        "up to group 55 (Military Specific). The pipeline allocates question generation effort "
        "across these major groups in proportion to each group's wage bill share.")

    doc.add_heading('2.3 Primary Occupation', level=2)
    body(doc,
        "A primary occupation, often shortened to just 'primary' in code and logs, is an occupation "
        "that was selected at run launch to be processed by the pipeline. The V19 run launched with "
        "490 primary occupations after filtering against the O*NET-SOC valid codes set (10 BLS "
        "aggregate codes were dropped because they do not exist in O*NET). Each primary is assigned "
        "to exactly one worker process and gets the full pipeline treatment: whitelist construction, "
        "source discovery, fetching, evidence card extraction, item building, nine verifier passes, "
        "and source mix gating. A primary that ends with at least the floor number of accepted "
        "items appears in the final bank under its own SOC code.")

    doc.add_heading('2.4 Reserve Occupation', level=2)
    body(doc,
        "A reserve occupation, or just 'reserve', is a backup occupation pre-selected from the "
        "same SOC major group as a primary. Reserves are picked by ranking all eligible occupations "
        "in a major group by wage bill in descending order and taking the next three after the "
        "primaries. Reserves never run during the initial parallel processing stage; they only "
        "run during the replacement phase if one of the primary occupations in their major group "
        "ended below the replacement trigger threshold (15 items in V19). When a reserve runs and "
        "yields enough items, it replaces the under yielded primary in the final bank, taking that "
        "primary's slot.")

    doc.add_heading('2.5 Whitelist (Per-Occupation Domain Whitelist)', level=2)
    body(doc,
        "A whitelist is a curated allowlist of source domains (websites) that the pipeline is "
        "allowed to fetch content from for a given occupation. Without a whitelist, a Google "
        "search query for 'Registered Nurse safety procedure' would surface millions of low quality "
        "or off topic pages including blogs, social media, marketing pages, and content farms. "
        "The whitelist constrains the discovery step to only return URLs from a specific set of "
        "authoritative publishers relevant to that occupation.")
    body(doc,
        "For a Registered Nurse, the whitelist typically contains domains like aacn.org (American "
        "Association of Critical Care Nurses), nursingworld.org (American Nurses Association), "
        "ncsbn.org (National Council of State Boards of Nursing), the relevant state board of "
        "nursing domains, and the federal regulatory baseline of cdc.gov, osha.gov, and bls.gov. "
        "For a Software Developer, the whitelist instead contains domains like ieee.org, acm.org, "
        "owasp.org, nist.gov, and so on. Each occupation has its own per occupation whitelist "
        "tailored to its professional context.")
    body(doc,
        "Mechanically, the whitelist is enforced inside the discover_urls function in "
        "pipeline.search_client. After the Serper API returns search results, every URL is checked "
        "against the per occupation whitelist; URLs whose domain (or a parent domain) does not "
        "appear in the whitelist are silently dropped before any fetch attempt. This per occupation "
        "filtering is the central mechanism the pipeline uses to enforce source quality at scale.")

    doc.add_heading('2.6 Wage Bill', level=2)
    body(doc,
        "Wage bill is a derived quantity computed as national employment multiplied by the "
        "occupation's annual wage value. It estimates the total labor income flowing through "
        "an occupation in the United States economy. The pipeline uses wage bill (rather than "
        "wage alone, or employment alone) as the central weight for SOC selection per Abhishek's "
        "April 30 call. The intuition is that an occupation's importance in the economy is best "
        "captured by how much money it represents in aggregate, which is what wage bill measures.")

    doc.add_heading('2.7 Item, Card, and Source', level=2)
    body(doc,
        "An item is a single MCQ in the final bank: a question stem, six options labeled A through "
        "F, a designated correct answer letter, plus metadata (source URL, source quote, occupation, "
        "quality tier). A card or evidence card is the intermediate artifact extracted from a "
        "source document before being shaped into an item; one source can yield multiple cards, "
        "and one card produces at most one item. A source refers to a single document (HTML page "
        "or PDF) fetched from a URL on the per occupation whitelist or the global academic allowlist.")

    # ───── 3. OCCUPATION SELECTION ─────
    doc.add_heading('3. Occupation Selection (Wage Bill Weighting)', level=1)

    doc.add_heading('3.1 Data Source: BLS OEWS 2024', level=2)
    body(doc,
        "The pipeline begins by loading the Bureau of Labor Statistics Occupational Employment "
        "and Wage Statistics (OEWS) national table for May 2024, downloaded from "
        "https://www.bls.gov/oes/special-requests/oesm24nat.zip. The Excel workbook is filtered "
        "to detailed occupations only by checking the O_GROUP column equals 'detailed', which "
        "yields 831 SOC codes after excluding aggregate roll ups (major group, minor group, "
        "broad occupation, and totals).")
    body(doc,
        "For each detailed occupation, the pipeline computes an annual wage value with a robust "
        "fallback chain: it first uses A_MEDIAN (annual median wage), then falls back to A_MEAN "
        "(annual mean wage) if median is suppressed, and finally falls back to H_MEDIAN multiplied "
        "by 2,080 hours per year. The OEWS data uses the codes asterisk, double asterisk, hash, "
        "and triple asterisk to indicate suppressed values; the loader treats these as NaN and "
        "the fallback chain handles the substitution.")
    body(doc,
        "The wage_bill metric, the central quantity used for selection, is computed as "
        "national employment multiplied by the annual wage value. This estimates the total "
        "labor income flowing through each occupation in the United States economy.")

    doc.add_heading('3.2 Plan Section 3.2 Proportional Allocation', level=2)
    body(doc,
        "The selection algorithm in pipeline.occupation_selection.select_proportional follows "
        "plan section 3.2 strictly. For each of the twenty three SOC major groups (codes 11 "
        "Management through 55 Military Specific), the algorithm computes the group's total "
        "wage_bill as the sum across all detailed occupations in that group, then computes the "
        "group's share of the national wage_bill total.")
    body(doc,
        "Slot allocation proceeds in five steps. Step one computes the raw proportional "
        "allocation by multiplying the target_total (500 for V19) by each group's wage_bill "
        "share, with a minimum of one slot per group. Step two enforces a floor of three slots "
        "per group with eligible occupations, ensuring no major group is excluded entirely. "
        "Step three applies a cap (80 slots per group for V19) to prevent any single group from "
        "dominating the bank. Step four trims the total back to target by removing slots from "
        "the largest group (without going below floor). Step five tops up if under target by "
        "adding slots to the highest wage_bill groups still under the cap.")
    body(doc,
        "Within each group, the algorithm ranks the eligible occupations by wage_bill in "
        "descending order and selects the top N as primaries, where N is the group's allocated "
        "slot count. An additional three reserve occupations per group are also selected, ranked "
        "immediately below the primaries by wage_bill. These reserves become candidates for the "
        "same rule replacement phase described in section 12.")

    doc.add_heading('3.3 V19 Specific Configuration', level=2)
    body(doc,
        "V19 was launched with target_total equal to 500, floor_per_group equal to 3, "
        "cap_per_group equal to 80, and reserves_per_group equal to 3. The selection produced "
        "510 raw primary candidates which were then filtered against the O*NET-SOC valid codes "
        "set; 10 BLS aggregate codes that did not appear in O*NET were dropped, leaving 490 "
        "primaries plus 36 reserves across all eligible SOC major groups.")

    # ───── 4. SOURCE WHITELIST CONSTRUCTION ─────
    doc.add_heading('4. Source Whitelist Construction', level=1)
    body(doc,
        "Recall from section 2.5 that a whitelist is a curated allowlist of source domains the "
        "pipeline is permitted to fetch content from. This section describes how the whitelist "
        "is built for each occupation.")

    doc.add_heading('4.1 Per-Occupation Domain Whitelist', level=2)
    body(doc,
        "For each selected occupation, the pipeline builds a per occupation whitelist of "
        "authoritative domains by combining four signals: the BLS Occupational Outlook Handbook "
        "domain mappings, an LLM generated list of relevant professional societies and state "
        "licensing boards given the occupation title, common federal regulatory bodies (CDC, "
        "OSHA, BLS), and manual additions from the prior pilot runs.")
    body(doc,
        "The whitelist build runs once per occupation and is cached to "
        "output/per_occupation_associations_v2.json. Whitelist construction has incremental "
        "save behavior: every five SOCs, the in progress whitelist is flushed to disk, so a "
        "restart resumes from the last saved checkpoint without re-querying.")

    doc.add_heading('4.2 Global Academic Domain Allowlist', level=2)
    body(doc,
        "In addition to per occupation whitelists, the pipeline maintains a global academic "
        "domain allowlist in pipeline.academic_sources.GLOBAL_ACADEMIC_DOMAINS. This set "
        "includes arxiv.org, ncbi.nlm.nih.gov and pmc.ncbi.nlm.nih.gov, plos.org, doi.org, "
        "openaire.eu, doaj.org, core.ac.uk, semanticscholar.org, openstax.org, libretexts.org, "
        "ocw.mit.edu, scholar.google.com, biorxiv.org, medrxiv.org, and the open access publisher "
        "domains biomedcentral.com, frontiersin.org, mdpi.com, hindawi.com, springeropen.com, "
        "europepmc.org, plus selected wiley.com, springer.com, sciencedirect.com, "
        "tandfonline.com, sagepub.com, oup.com, and nature.com URLs that resolve through "
        "Unpaywall as open access.")
    body(doc,
        "URLs from any global academic domain bypass the per occupation whitelist filter, "
        "allowing scientific literature to enter the bank for any occupation regardless of "
        "whether the specific domain was named in that occupation's whitelist.")

    # ───── 5. SOURCE DISCOVERY ─────
    doc.add_heading('5. Source Discovery', level=1)

    doc.add_heading('5.1 Round 0: Academic Discovery', level=2)
    body(doc,
        "Before invoking Serper for general web discovery, each occupation runs a round 0 "
        "academic discovery step that calls three free APIs in sequence: arXiv, PubMed Central, "
        "and Crossref plus Unpaywall. The results bypass the per occupation whitelist filter via "
        "the global academic domain allowlist.")
    body(doc,
        "The arXiv lookup applies only to STEM occupations that have been pre mapped to arXiv "
        "subject categories in pipeline.academic_sources.SOC_TO_ARXIV_CATS. This mapping covers "
        "58 SOCs across Computer and Mathematical (group 15), Architecture and Engineering "
        "(group 17), Life, Physical, and Social Science (group 19), and parts of Healthcare "
        "Practitioners (group 29). For example, SOC 15-1252.00 Software Developers maps to "
        "arXiv categories cs.SE, cs.PL, and cs.SY; SOC 15-2041.00 Statisticians maps to "
        "stat.ME and stat.AP. The arXiv API call combines the occupation title and the relevant "
        "categories in a single Atom feed query, retrieving up to 8 PDF URLs per occupation.")
    body(doc,
        "PubMed Central lookup applies to healthcare occupations (SOC major groups 29 and 31). "
        "The lookup uses NCBI E utilities: first esearch.fcgi against the pmc database with "
        "the query 'occupation_title AND open access[filter]' to retrieve up to 8 PMC IDs, then "
        "esummary.fcgi to fetch titles and DOIs for those IDs.")
    body(doc,
        "Crossref plus Unpaywall is the third free API discovery channel and applies to all "
        "occupations regardless of SOC group. The Crossref works endpoint is queried with the "
        "occupation title, type filter set to journal article, and a result count of 4 times the "
        "max_per_source so that we have enough candidates to filter through Unpaywall. Each "
        "returned DOI is then resolved through the Unpaywall API, which returns either a "
        "free open access PDF URL via best_oa_location.url_for_pdf, the landing page URL via "
        "best_oa_location.url, or null. Only DOIs with a non null Unpaywall response are kept.")
    speaker_note(doc,
        "The Crossref plus Unpaywall pairing is what gives V19 access to peer reviewed articles "
        "from major commercial publishers including Wiley, Elsevier, Springer, Sage, and Taylor "
        "and Francis, but only the open access subset. This is critical for healthcare and "
        "engineering occupations where the canonical literature lives behind paywalls. The "
        "Unpaywall API has a free tier of 100,000 requests per day with no key required.")

    doc.add_heading('5.2 Rounds 1 through 10: Serper Backed Discovery', level=2)
    body(doc,
        "After round 0 academic discovery, the pipeline runs ten Serper backed discovery rounds "
        "per occupation. Round 1 is a general discovery round that combines the occupation title "
        "with nine document type templates: standard, guideline, manual, procedure, code of "
        "ethics, certification handbook, exam content outline, competency, and filetype:pdf. "
        "Each template runs against every domain in the per occupation whitelist as a Google site: "
        "operator query.")
    body(doc,
        "Rounds 2 through 10 are topical aspect rounds. Each round picks one occupation relevant "
        "aspect keyword (for example, 'patient safety', 'electrical hazard', 'data privacy', "
        "'quality control') and combines it with the occupation title across each whitelisted "
        "domain plus a small set of .gov and .edu fallback queries. Topical rounds run only if "
        "the per occupation item count remains below the target_items threshold.")
    body(doc,
        "Per occupation Serper budget is capped at 30 queries per discover_urls call "
        "(MAX_DISCOVERY_QUERIES_PER_OCCUPATION), which translates to up to 300 Serper queries "
        "across all 10 rounds for an occupation that exhausts its budget.")

    doc.add_heading('5.3 IEEE Xplore Filter (V19 Fix)', level=2)
    body(doc,
        "V19 introduced a paywalled URL filter at the discover_urls stage in "
        "pipeline.search_client._is_known_paywalled_url. The filter drops any URL matching the "
        "regular expression 'ieeexplore\\.ieee\\.org/iel\\d/' before the URL enters the fetch "
        "queue. This pattern matches IEEE Xplore deep link PDFs in the iel5 and iel7 subtrees, "
        "all of which are subscription only and previously caused the worker to waste "
        "ScrapingBee premium credits returning only the IEEE authentication landing page. "
        "Open access IEEE papers using the /document/ URL pattern are still allowed through.")

    doc.add_heading('5.4 PubMed Central efetch Fix (V19 Fix)', level=2)
    body(doc,
        "Prior to V19, the PubMed Central discovery code returned URLs of the form "
        "'https://www.ncbi.nlm.nih.gov/pmc/articles/PMC<id>/pdf/' which, when fetched, returned "
        "a roughly 1.8 kilobyte HTML interstitial page reading 'Preparing to download'. The "
        "interstitial requires JavaScript execution to redirect to the actual PDF file, but the "
        "pipeline's source_fetcher uses a non JavaScript urllib client by default. As a result, "
        "all 448 PMC fetch attempts in the supplement run returned 'too_short_80' errors and "
        "yielded zero items.")
    body(doc,
        "V19 fixes this by routing PMC URLs through the NCBI efetch XML API instead. The new "
        "function pipeline.academic_sources.fetch_pmc_efetch_text constructs a URL of the form "
        "'https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pmc&id=<id>&rettype=xml&retmode=text', "
        "which returns the full JATS formatted XML article body (typically 30 to 70 kilobytes "
        "of source text). The XML is then stripped of tags via regular expressions and returned "
        "as plain text. The fetcher in source_fetcher.fetch_and_clean detects PMC URLs at the "
        "top of the function via the helper extract_pmc_id_from_url and routes them through the "
        "efetch path, bypassing the JavaScript interstitial entirely.")

    # ───── 6. SOURCE FETCHING ─────
    doc.add_heading('6. Source Fetching', level=1)

    doc.add_heading('6.1 Direct Fetch (urllib)', level=2)
    body(doc,
        "The default fetch path uses Python's built in urllib with a Mozilla compatible "
        "User Agent string. The fetcher does not pre check with HEAD because many .gov and "
        "association sites reject HEAD with non 405 codes but accept GET fine. Fetched bytes "
        "are then run through one of three extractors: PyMuPDF for PDFs, an HTML cleanup regex "
        "for HTML pages, or the new efetch XML extractor for PMC URLs.")

    doc.add_heading('6.2 ScrapingBee Premium Routing', level=2)
    body(doc,
        "The pipeline maintains a hard coded set of paywalled or anti bot domains in "
        "pipeline.source_fetcher.PAYWALLED_OR_ANTIBOT_DOMAINS. This set includes 61 specific "
        "domains across engineering societies (asce.org, asme.org, ieee.org, etc.), standards "
        "bodies (astm.org, ansi.org, iso.org, nfpa.org), healthcare bodies (usp.org, asha.org, "
        "aota.org, apta.org), finance and legal bodies (aicpa-cima.com, americanbar.org, "
        "cfainstitute.org), computing societies (acm.org, isc2.org, isaca.org), and several "
        "California state licensing boards.")
    body(doc,
        "When a fetch URL's domain matches the paywalled set, the pipeline skips the direct "
        "fetch and goes straight to ScrapingBee with premium_proxy=True and render_js=True. "
        "If that fails, it retries with basic ScrapingBee (no premium, no JS), then falls back "
        "to direct fetch. ScrapingBee is a paid third party scraping service that uses "
        "residential proxies and a headless Chromium engine to bypass bot detection and "
        "JavaScript walled pages.")

    # ───── 7. CONTENT PROCESSING (CHUNKING) ─────
    doc.add_heading('7. Content Processing: Chunking and Section Extraction', level=1)
    body(doc,
        "After a source is successfully fetched, the raw document text is processed into a "
        "structured form before being passed to the evidence card extractor. This processing "
        "stage uses regular expressions for section detection and segmentation, then chunking "
        "to break the document into LLM friendly passages.")

    doc.add_heading('7.1 Text Extraction', level=2)
    body(doc,
        "Three extraction paths run depending on the source content type. PDFs are processed "
        "through PyMuPDF (the pymupdf module), which walks the document page by page, calls "
        "page.get_text('text') on each, and concatenates the results. HTML pages are processed "
        "through a regex pipeline that strips script tags via re.sub(r'<script[\\\\s\\\\S]*?</script>', "
        "'', raw, flags=re.I), strips style tags similarly, removes all remaining HTML tags via "
        "re.sub(r'<[^>]+>', ' ', text), and finally collapses whitespace via re.sub(r'\\\\s+', "
        "' ', text). PMC articles are processed through the efetch XML path described in "
        "section 5.4, with regex driven JATS tag stripping using the same whitespace collapse pattern.")

    doc.add_heading('7.2 Section Header Detection (Regex)', level=2)
    body(doc,
        "Once raw text is extracted, the processor uses regex pattern matching to detect section "
        "boundaries within the document. For HTML sources, headings such as h1 through h6 are "
        "preserved during extraction as 'SECTION:' markers. For PDFs, regex patterns identify "
        "common section header conventions: numbered headings like '\\\\d+\\\\.\\\\s+[A-Z][a-z]+', "
        "all caps lines longer than 5 characters as standalone paragraphs, and known boilerplate "
        "section titles such as 'Abstract', 'Introduction', 'Methods', 'Results', 'Discussion', "
        "'References', 'Procedures', 'Recommendations', and 'Standards'.")
    body(doc,
        "Date and version regex patterns also run during this stage. The patterns "
        "_HTML_PUB_DATE_RES match meta tags carrying article:published_time, datePublished, "
        "publish_date, pubdate, dc.date, and dc.date.issued; "
        "_HTML_VERSION_RES match meta tags carrying version, edition, or dc.version. PDF "
        "metadata is parsed using regex against the PDF date format 'D:YYYYMMDDHHmmSS', "
        "extracting publication date and version when present in the document XMP metadata.")

    doc.add_heading('7.3 Chunking for Evidence Extraction', level=2)
    body(doc,
        "After section detection, the document is chunked into passages so that most of the "
        "document's information can be processed rather than just the leading portion. Chunks "
        "are sized to 600 to 1,000 tokens (approximately 2,400 to 4,000 characters), with 100 "
        "to 150 token overlap between adjacent chunks to preserve cross boundary context. "
        "Each chunk carries forward the section title, page number (for PDFs), and source URL "
        "anchor when available, so that evidence cards can later cite the precise location.")
    body(doc,
        "The chunking pass enables coverage of late document sections that a simple leading "
        "truncation would miss, such as the procedural details deep in a clinical guideline "
        "or the specific numeric thresholds at the end of a long technical standard. Each chunk "
        "is independently scored for relevance against the occupation's task descriptors before "
        "being passed to the evidence card extractor, so chunks unrelated to the occupation "
        "(boilerplate, unrelated appendices) are filtered out.")
    body(doc,
        "Minimum document length is set to MIN_DOC_CHARS = 1,500 characters of extracted text. "
        "Documents shorter than this minimum are rejected as too_short_<N> errors before being "
        "sent to the chunker, since they typically indicate a fetch failure that returned a "
        "redirect page or an authentication wall rather than the actual document.")
    speaker_note(doc,
        "The chunking and section detection logic is what allows the pipeline to extract evidence "
        "from anywhere in a long document. For long technical standards (NFPA, ASTM), clinical "
        "guidelines, and academic papers, the relevant procedural detail is often deep in the "
        "document rather than in the abstract or executive summary. Section header preservation "
        "through regex pattern matching means that an item's source attribution can later cite "
        "'Section 4.2 Equipment Calibration' rather than just the source URL.")

    # ───── 8. EVIDENCE CARD EXTRACTION ─────
    doc.add_heading('8. Evidence Card Extraction', level=1)
    body(doc,
        "Each chunk of the processed document is sent to pipeline.evidence_cards.extract_cards "
        "along with the occupation title and SOC code. The extractor is an OpenAI gpt-4o call "
        "that returns a JSON list of evidence cards (recall from section 2.7 that a card is the "
        "intermediate artifact preceding an item). Each card contains: a source quote of one to "
        "three contiguous sentences from the document, a task or procedure described in the "
        "quote, the relevant occupation field, and a quality tier label (A for primary regulatory "
        "or peer reviewed, B for professional society or state board, C for general industry or "
        "trade press).")
    body(doc,
        "Cards that fail JSON parsing or that do not contain a verbatim source_quote of the "
        "minimum length are rejected at extraction time. Each accepted card is then handed to "
        "the item builder for MCQ construction.")

    # ───── 9. ITEM BUILDING ─────
    doc.add_heading('9. Item Building', level=1)
    body(doc,
        "Each evidence card is sent to pipeline.item_generator.build_item, which is an OpenAI "
        "gpt-4o call that produces a six option multiple choice question. The standard format "
        "uses options A, B, C, and D as content bearing options, with E always 'All of the "
        "above' and F always 'None of the above'. The correct answer letter is one of A, B, "
        "C, or D in V19.")
    body(doc,
        "The builder enforces several initial constraints: each option must be roughly the "
        "same length as the others (no obvious giveaways via length), must be semantically "
        "distinct from the correct answer, and must avoid named source giveaway phrases such "
        "as 'According to NFPA' or 'Per the AICPA Code'. Items that fail these constraints are "
        "logged as 'build fail [option_X_giveaway_phrase]' and the card is discarded.")

    # ───── 10. NINE VERIFIER PASSES ─────
    doc.add_heading('10. Nine Verifier Passes', level=1)
    body(doc,
        "Every successfully built item runs through a sequence of nine verifier passes "
        "implemented in pipeline.item_verifier.verify_item. Six of these passes have a "
        "regenerate not reject path: if the pass fails, the item is sent back to a targeted "
        "regeneration call rather than being discarded immediately. After regeneration, the "
        "item re enters the verifier pipeline.")

    doc.add_heading('10.1 Pass 1: Source Entailment', level=2)
    body(doc,
        "Pass 1 calls an LLM verifier (currently gpt-4o or claude-sonnet) with the source quote "
        "and asks whether the correct answer option is fully entailed by the source. The verifier "
        "returns a JSON verdict with entailment status and a brief reason. Items where the "
        "correct option is not entailed are rejected.")

    doc.add_heading('10.2 Pass 2: Distractor Plausibility (with Pass 2a Regen)', level=2)
    body(doc,
        "Pass 2 checks that each distractor (incorrect option) is at least somewhat plausible "
        "in the occupation context. A distractor that is wildly off topic makes the correct "
        "answer too easy to identify by elimination. If a distractor is judged too implausible, "
        "Pass 2a is invoked: a regeneration call replaces just that distractor with a more "
        "plausible alternative, and the item is rebuilt with the new option set before "
        "re entering verification.")

    doc.add_heading('10.3 Pass 3: Occupation Alignment', level=2)
    body(doc,
        "Pass 3 verifies that the question scenario is genuinely something the named occupation "
        "would do as part of their job. For example, a question about general statistics that "
        "could apply to any analyst would fail this pass for a Statistician occupation if the "
        "scenario does not involve specifically statistical work. Items that fail are rejected.")

    doc.add_heading('10.4 Pass 4: Question Specificity (with Regen)', level=2)
    body(doc,
        "Pass 4 checks whether the question is too generic. For example, 'What should the "
        "worker do?' without a specific scenario. If the question is too generic, the regen "
        "path calls the LLM to rewrite the question stem with more concrete scenario details "
        "(a specific patient, equipment, time pressure, or constraint).")

    doc.add_heading('10.5 Pass 5: Distractor Distinctness (with Regen)', level=2)
    body(doc,
        "Pass 5 verifies that the four content bearing options are semantically distinct from "
        "each other. Two distractors that paraphrase the same underlying answer make the "
        "elimination logic ambiguous. The regen path rewrites any duplicated distractor with a "
        "different alternative.")

    doc.add_heading('10.6 Pass 6: Format and Length Sanity', level=2)
    body(doc,
        "Pass 6 enforces structural sanity: option lengths within 0.5 to 2 times of each other, "
        "no option exceeding 350 characters, and no malformed JSON. Pass 6b additionally checks "
        "for named source giveaway phrases in the correct option (for example, 'Follow the AORN "
        "Guideline'), which would let a test taker guess the correct option just because it "
        "names an authoritative sounding source. Pass 6b has a regen path that rewrites the "
        "correct option without the source name.")

    doc.add_heading('10.7 Pass 7: Difficulty Pretest (3 Models, 3 Providers)', level=2)
    body(doc,
        "Pass 7 evaluates the item against three closed book models in parallel: gpt-4o-mini "
        "from OpenAI, claude-haiku-4.5 from Anthropic, and gemini-2.5-flash from Google. Each "
        "model receives only the question stem and the six options, with no source context. "
        "The pass records n_correct (how many of the three models got the answer right) and "
        "applies two rejection rules.")
    body(doc,
        "If all three models answer correctly, the item is rejected as 'too_easy', since these "
        "items do not differentiate models and add no measurement signal. If all three models "
        "answer incorrectly AND the verifier confidence on the correct answer is low, the item "
        "is rejected as 'too_hard_low_conf', likely a poorly formed question rather than a "
        "genuinely hard one. Items where two of three models are correct are kept as 'medium' "
        "difficulty; one of three correct is 'hard'; and zero with high verifier confidence is "
        "also kept.")

    doc.add_heading('10.8 Pass 8: LLM Eliminability Judge (with Regen)', level=2)
    body(doc,
        "Pass 8 asks an LLM judge whether each distractor could be eliminated by general world "
        "knowledge alone, without consulting the source quote. A distractor that is 'obviously "
        "wrong by general knowledge' (for example, includes a factual claim contradicted by "
        "common sense) makes the question too easy for any reasonably informed test taker. "
        "When Pass 8 flags an eliminable distractor, the regen path rewrites that distractor "
        "into something more defensible: typically a plausible but wrong procedure or a true "
        "statement that does not actually answer the question.")

    doc.add_heading('10.9 Pass 9: Correct Option Specificity (with Regen)', level=2)
    body(doc,
        "Pass 9 is the latest addition and addresses a specific failure mode where the correct "
        "option is more vague or generic than the distractors. The classic example is a "
        "correct option reading 'Adhere to basic safety requirements' next to distractors "
        "naming specific tools like 'Use a fume hood' and 'Use a glove box'. A reasonable "
        "test taker would pick the more specific options because they sound more concrete, "
        "even though the source actually says the generic statement.")
    body(doc,
        "The Pass 9 prompt explicitly enumerates vague qualifiers ('appropriate', 'adequate', "
        "'proper', 'basic', 'standard', 'necessary', 'applicable', 'relevant') and platitude "
        "patterns ('adhere to safety requirements', 'follow guidelines', 'ensure compliance', "
        "'use best practices', 'consult an expert', 'follow procedures'). Items where the "
        "correct option matches these patterns and is less specific than the distractors are "
        "sent to a regeneration call that rewrites the correct option with concrete details "
        "from the source quote: a specific tool, threshold, document name, or procedure.")

    # ───── 11. ITEM ACCEPTANCE GATES ─────
    doc.add_heading('11. Item Acceptance Gates', level=1)

    doc.add_heading('11.1 Jaccard Deduplication', level=2)
    body(doc,
        "Before an item is added to a SOC's items list, the pipeline tokenizes the question "
        "stem and computes Jaccard similarity against every previously accepted item for that "
        "SOC. Items with Jaccard similarity above DUP_JACCARD_THRESHOLD (0.6 in V19) are "
        "rejected as duplicates.")

    doc.add_heading('11.2 Source Mix Acceptance Gate', level=2)
    body(doc,
        "After all nine verifier passes succeed, the item passes through a final source mix "
        "acceptance gate that enforces three rules.")
    body(doc,
        "Rule one: per SOC source cap. No single source domain may exceed 30 percent of the "
        "items for that SOC. If accepting this item would push the dominant domain over 30 "
        "percent, the item is rejected as 'source-cap: <domain> would be N percent'.")
    body(doc,
        "Rule two: CDC, OSHA, and BLS canonical limits. For SOC major groups where these are "
        "the canonical authority (for example, CDC for healthcare safety, OSHA for industrial "
        "safety, BLS for occupation definitions), a higher cap of 40 to 50 percent applies. "
        "For groups where they are tangential, the cap is lower (15 to 20 percent), preventing "
        "dilution.")
    body(doc,
        "Rule three: blocked sources. A small set of low quality domains (link aggregators, "
        "content farms) are blocked entirely.")

    # ───── 12. PER-SOC LIMITS ─────
    doc.add_heading('12. Per SOC Operational Limits', level=1)
    body(doc,
        "Each SOC processes under three hard caps: MAX_FAILURES_PER_OCCUPATION = 12 (extraction "
        "or quality failures before the SOC gives up), MAX_FETCH_FAILURES_PER_OCCUPATION = 35 "
        "(fetch failures before stopping), and MAX_DISCOVERY_QUERIES_PER_OCCUPATION = 30 (per "
        "discover_urls call). The target item count is 20 per SOC, with discovery rounds "
        "stopping early once 20 items are accepted.")

    # ───── 13. REPLACEMENT PHASE ─────
    doc.add_heading('13. Replacement Phase (Plan §3.4)', level=1)
    body(doc,
        "After all primary SOCs finish processing, the launcher runs a replacement phase that "
        "backfills under yielded primaries with reserve occupations from the same SOC major "
        "group. A primary is considered under yielded if it ends with fewer than 15 items "
        "(the V19 replacement trigger) or has a single source dominating more than the "
        "source mix cap.")
    body(doc,
        "For each under yielded primary, the launcher iterates through the reserves list for "
        "that SOC major group (ranked by wage_bill, top down) and runs a fresh full pipeline "
        "pass on each reserve until one yields at least 15 items. The successful reserve "
        "replaces the primary in the bank; the primary is dropped. If all reserves are "
        "exhausted without reaching the floor, the primary is also dropped (no replacement) "
        "and the SOC slot is left vacant.")

    # ───── 14. POST-REPLACEMENT FILTER ─────
    doc.add_heading('14. Post Replacement Floor Filter', level=1)
    body(doc,
        "The final stage of the pipeline applies a hard floor filter: any SOC ending with "
        "fewer than 5 items (V19 floor) is dropped from the published bank entirely. This "
        "ensures every SOC in the final CSV has the minimum coverage needed for evaluation.")

    # ───── 15. OPERATIONAL DETAILS ─────
    doc.add_heading('15. Operational Details', level=1)

    doc.add_heading('15.1 Quota Aware Resume Logic', level=2)
    body(doc,
        "The orchestrator includes a quota aware resume logic that distinguishes between four "
        "states when re running an SOC. State one: items_kept >= 1, the SOC is preserved as is. "
        "State two: items_kept = 0 AND extract_failures > 95 percent of urls_fetched, the SOC "
        "is retried with fresh stats (signature of OpenAI quota exhaustion mid run). State "
        "three: items_kept = 0 AND fetched > 0, the SOC is left as genuine zero yield. State "
        "four: items_kept = 0 AND fetched = 0, the SOC is re processed (signature of Serper "
        "outage during the original run).")

    doc.add_heading('15.2 Worker Sharding and Parallelism', level=2)
    body(doc,
        "V19 launched 32 worker processes in parallel via launch_diversity50.py. Each worker "
        "is a separate Python process invoking run_pilot20.py with its own chunk CSV of "
        "approximately 15 to 16 occupations. Workers run independently with their own items.jsonl "
        "and stats.json output files. After all workers exit, the launcher merges all worker "
        "outputs into a run level bank, then runs the replacement phase against the merged bank.")

    doc.add_heading('15.3 Recovery Run for Serper Damaged Workers', level=2)
    body(doc,
        "When the supplement run experienced a Serper credit exhaustion event mid run, 12 of "
        "the 32 supplement worker processes hit Serper HTTP 400 'not enough credits' and "
        "permanently marked the API key as exhausted in their _EXHAUSTED_KEYS set. Even after "
        "credits were restored externally, those workers continued returning empty Serper "
        "results because the in memory exhausted key flag is not cleared.")
    body(doc,
        "The recovery flow identified the 41 SOCs damaged by this state (SOCs where "
        "urls_discovered was zero across all attempted rounds), then spawned 12 fresh Python "
        "processes via launch_recovery.py to re process those SOCs from scratch. Recovery items "
        "merged into the v19 base bank deduped by evidence_id.")

    doc.add_heading('15.4 Finalize Daemon', level=2)
    body(doc,
        "The finalize_v19_daemon.sh shell script polls the three launcher PIDs (v19, supplement, "
        "recovery) every 60 seconds and triggers the export step when either all three exit OR "
        "when five or more worker logs show OpenAI quota errors within a three minute window. "
        "The export step performs a final merge sweep of all worker items.jsonl files, then "
        "runs export_v19_final.py to produce the final bank CSV (with SOCs with at least 5 "
        "items only) and the source distribution figure.")

    # ───── 16. FINAL BANK STATISTICS ─────
    doc.add_heading('16. Final Bank Statistics', level=1)
    body(doc,
        "The V19 final bank contains 1,330 items across 89 occupations (each with at least 5 "
        "items), drawn from 347 unique source domains. Source category distribution: "
        "Professional societies and associations 63.4 percent, Federal .gov baseline (BLS, OSHA, "
        "CDC) 20.9 percent, State licensing boards 8.8 percent, Federal .gov other 5.8 percent, "
        "and Academic / .edu (arXiv, PMC, OA journals) 1.1 percent. CDC is the single largest "
        "domain at 16.5 percent of the bank, down from 31 percent in pilot v4.")
    speaker_note(doc,
        "When presenting source distribution, lead with the CDC drop from 31 to 16.5 percent "
        "as the headline metric. Then point out that the bank now includes academic literature "
        "for the first time (arXiv, PMC, Crossref Unpaywall), which was absent in v4. The 347 "
        "unique source domains (versus 23 in v4) is the second key metric. Diversity is the "
        "central concern in Abhishek's review feedback and we should be able to defend it "
        "quantitatively.")

    # Save
    out = Path("v19_pipeline_documentation.docx")
    doc.save(out)
    print(f"Wrote {out.absolute()}")

    # Copy to final_bank_results folder
    dest = Path("final_bank_results") / out.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(out, dest)
    print(f"Copied to {dest.absolute()}")


if __name__ == "__main__":
    main()
