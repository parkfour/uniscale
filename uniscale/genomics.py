import base64
import json
import logging
import os
import re
import zipfile
import zlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uniscale.models import GenomicProfile, GenomicVariant
from uniscale.config import vault_db_path

logger = logging.getLogger(__name__)

CURATED_SNPS = {
    "rs429358": {
        "gene": "APOE",
        "name": "Apolipoprotein E (Cys112Arg)",
        "category": "cardiovascular",
        "description": "Determines ApoE isoform alongside rs7412 (ε2, ε3, ε4 alleles). Key driver of lipid clearance and neurovascular longevity."
    },
    "rs7412": {
        "gene": "APOE",
        "name": "Apolipoprotein E (Arg158Cys)",
        "category": "cardiovascular",
        "description": "Determines ApoE isoform alongside rs429358. ε3 is wild-type; ε2 enhances LDL clearance; ε4 increases atherogenic particles."
    },
    "rs1801133": {
        "gene": "MTHFR",
        "name": "MTHFR C677T (Ala222Val)",
        "category": "longevity",
        "description": "Regulates 5-MTHF synthesis for homocysteine remethylation to methionine and cellular DNA methylation."
    },
    "rs1801131": {
        "gene": "MTHFR",
        "name": "MTHFR A1298C (Glu429Ala)",
        "category": "longevity",
        "description": "Secondary MTHFR variant influencing tetrahydrobiopterin (BH4) recycling and nitric oxide synthesis."
    },
    "rs2802292": {
        "gene": "FOXO3",
        "name": "FOXO3 Longevity Variant",
        "category": "longevity",
        "description": "Master regulator of cellular autophagy, DNA repair, and stem cell homeostasis in centenarians."
    },
    "rs10757278": {
        "gene": "CDKN2A/B (9p21.3)",
        "name": "9p21 Coronary Artery Disease Locus",
        "category": "cardiovascular",
        "description": "Strongest known genetic determinant of coronary heart disease and vascular smooth muscle senescence."
    },
    "rs4149056": {
        "gene": "SLCO1B1",
        "name": "OATP1B1 Statin Transporter (*5)",
        "category": "pharmacogenomics",
        "description": "Controls hepatic uptake of statin medications (simvastatin, atorvastatin). Variants cause elevated plasma statin levels."
    },
    "rs4986893": {
        "gene": "CYP2C19",
        "name": "CYP2C19*3 (Loss of Function)",
        "category": "pharmacogenomics",
        "description": "Key loss-of-function allele causing intermediate/poor bioactivation of clopidogrel (Plavix) and delayed PPI metabolism."
    },
    "rs4244285": {
        "gene": "CYP2C19",
        "name": "CYP2C19*2 (Loss of Function)",
        "category": "pharmacogenomics",
        "description": "CYP450 enzyme responsible for bioactivation of clopidogrel (Plavix) and clearance of proton pump inhibitors."
    },
    "rs762551": {
        "gene": "CYP1A2",
        "name": "CYP1A2*1F (Caffeine Metabolism)",
        "category": "metabolism",
        "description": "Hepatic cytochrome P450 enzyme clearing caffeine, paraxanthine, and dietary heterocyclic amines."
    },
    "rs1815739": {
        "gene": "ACTN3",
        "name": "Alpha-Actinin-3 (R577X)",
        "category": "fitness",
        "description": "Structural fast-twitch Z-line protein determining power/sprint vs aerobic endurance muscular adaptations."
    },
    "rs8177374": {
        "gene": "TIRAP",
        "name": "TIRAP S180L (Immune Resilience)",
        "category": "longevity",
        "description": "Toll-like receptor adaptor protein conferring innate protective resistance against diverse systemic bacterial pathogens."
    },
    "rs6323": {
        "gene": "MAOA",
        "name": "Monoamine Oxidase A (R297R)",
        "category": "metabolism",
        "description": "X-linked enzyme degrading dopamine, norepinephrine, and serotonin; modulates neurochemical stress reactivity."
    },
    "rs6025": {
        "gene": "F5",
        "name": "Factor V Leiden",
        "category": "cardiovascular",
        "description": "Primary hereditary hypercoagulability / deep vein thrombosis genetic screening locus."
    },
    "rs1799963": {
        "gene": "F2",
        "name": "Prothrombin G20210A",
        "category": "cardiovascular",
        "description": "Second most common genetic risk factor for venous thromboembolism and arterial clotting."
    },
    "rs1800562": {
        "gene": "HFE",
        "name": "Hemochromatosis C282Y",
        "category": "metabolism",
        "description": "Major hereditary iron overload gene leading to systemic tissue ferritin accumulation if mutated."
    },
    "rs1799945": {
        "gene": "HFE",
        "name": "Hemochromatosis H63D",
        "category": "metabolism",
        "description": "Secondary modifier variant for hereditary iron regulation."
    },
    "rs4680": {
        "gene": "COMT",
        "name": "COMT Val158Met (Catecholamine Turnover)",
        "category": "neurobiology",
        "description": "Regulates prefrontal cortex dopamine degradation. Met/Met (AA) confers elevated baseline dopamine, executive focus, and working memory ('Worrier' phenotype)."
    },
    "rs6265": {
        "gene": "BDNF",
        "name": "BDNF Val66Met (Neuroplasticity)",
        "category": "neurobiology",
        "description": "Modulates activity-dependent BDNF secretion, hippocampal plasticity, and exercise-induced neurogenesis."
    },
    "rs9939609": {
        "gene": "FTO",
        "name": "FTO (Appetite & Metabolic Satiety)",
        "category": "metabolism",
        "description": "Influences central ghrelin signaling and appetite responsiveness; highly responsive to dietary protein."
    }
}


def extract_promethease_snps_from_zip(zip_path: str) -> Dict[str, Dict[str, Any]]:
    """
    Decompresses and extracts rich SNP dictionaries from a Promethease zip archive.
    """
    if not os.path.exists(zip_path):
        raise FileNotFoundError(f"Promethease file not found: {zip_path}")

    with zipfile.ZipFile(zip_path, "r") as z:
        html = z.read("promethease.html").decode("utf-8", errors="ignore")

    chunks = re.findall(r"decompressString\(\x27([^\x27]+)\x27\)", html)
    snps: Dict[str, Dict[str, Any]] = {}

    for chunk in chunks:
        try:
            raw = base64.b64decode(chunk)
            items = json.loads(zlib.decompress(raw).decode("utf-8"))
            for item in items:
                if isinstance(item, dict):
                    rs = (item.get("rsnum") or item.get("name") or "").lower()
                    if rs:
                        snps[rs] = item
        except Exception as e:
            logger.debug(f"Chunk decompression error: {e}")

    logger.info(f"Extracted {len(snps)} annotated SNPs from {zip_path}")
    return snps


def _raw_dna_fields(line: str) -> Optional[tuple]:
    """
    (rsid, allele1, allele2) from one line of a consumer raw-data export, or None for
    comments/headers. Handles AncestryDNA (rsid chrom pos allele1 allele2), 23andMe
    (rsid chrom pos "AG") and MyHeritage / FamilyTreeDNA quoted CSV ("rsid","chrom","pos","AG").
    """
    if line.startswith("#"):
        return None
    parts = line.replace(",", " ").replace('"', " ").split()
    if len(parts) < 4 or not parts[2].isdigit():
        return None
    if len(parts) >= 5:
        a1, a2 = parts[3], parts[4]
    else:
        a1, a2 = parts[3][:1], parts[3][1:]
    return parts[0], a1.upper(), a2.upper()


def extract_ancestry_snps_from_zip(zip_path: str) -> Dict[str, Dict[str, Any]]:
    """
    Extracts raw microarray SNPs from a consumer DNA zip archive: AncestryDNA (660k+ SNPs),
    23andMe or MyHeritage / FamilyTreeDNA.
    """
    if not os.path.exists(zip_path):
        raise FileNotFoundError(f"Raw DNA file not found: {zip_path}")

    snps: Dict[str, Dict[str, Any]] = {}
    with zipfile.ZipFile(zip_path, "r") as z:
        data_files = [n for n in z.namelist() if n.lower().endswith((".txt", ".csv"))]
        target = next((n for n in data_files if "ancestry" in n.lower()), data_files[0] if data_files else None)
        if not target:
            raise ValueError(f"No text data file found in {zip_path}")

        with z.open(target) as f:
            for raw in f:
                fields = _raw_dna_fields(raw.decode("ascii", errors="ignore"))
                if not fields:
                    continue
                rsnum, a1, a2 = fields
                snps[rsnum.lower()] = {
                    "rsnum": rsnum,
                    "geno": f"({a1};{a2})",
                    "allele1": a1,
                    "allele2": a2,
                    "magnitude": 0.0,
                    "repute": "Neutral",
                    "genosummary": ""
                }

    logger.info(f"Extracted {len(snps):,} raw microarray SNPs from archive {zip_path}")
    return snps


def clean_geno(geno: Optional[str]) -> str:
    if not geno:
        return ""
    return geno.replace("(", "").replace(")", "").replace(";", "").replace("/", "").strip().upper()


def interpret_apoe(g_429358: str, g_7412: str) -> Dict[str, str]:
    g_429358 = "".join(sorted(g_429358.upper()))
    g_7412 = "".join(sorted(g_7412.upper()))

    if "C" in g_429358 and "T" in g_7412:
        isoform = "ε2/ε4 (Neutral / Mixed)"
        significance = "Intermediate lipid clearance; balanced cardiovascular profile."
        rec = "Maintain optimal Mediterranean lipid ratio; monitor ApoB and LDL particle count annually."
    elif "C" in g_429358:
        if g_429358 == "CC":
            isoform = "ε4/ε4 (Elevated Risk)"
            significance = "High sensitivity to saturated dietary fats; increased lifetime atherogenic particle exposure."
            rec = "Prioritize monounsaturated fats (extra virgin olive oil); limit butter/coconut oils; target ApoB < 70 mg/dL."
        else:
            isoform = "ε3/ε4 (Mildly Elevated)"
            significance = "Moderate elevation in LDL atherogenicity; responsive to dietary fatty acid composition."
            rec = "Maintain high-fiber intake and aerobic conditioning; monitor ApoB and hs-CRP."
    elif "T" in g_7412:
        if g_7412 == "TT":
            isoform = "ε2/ε2 (Cardioprotective / Low LDL)"
            significance = "Rapid LDL hepatic clearance, lower cardiovascular hazard, potential mild hypertriglyceridemia."
            rec = "Keep refined carbohydrate intake modest to prevent triglyceride elevation."
        else:
            isoform = "ε2/ε3 (Cardioprotective)"
            significance = "Youthful lipid clearance dynamics and low baseline LDL."
            rec = "Optimal baseline; maintain balanced dietary protocol."
    else:
        isoform = "ε3/ε3 (Optimal Wild-Type)"
        significance = "Neutral population baseline (60% prevalence); optimal balance of neuroprotection and lipid turnover."
        rec = "Standard evidence-based longevity protocol; baseline lipid clearance is healthy."

    return {
        "isoform": isoform,
        "clinical_significance": significance,
        "recommendation": rec
    }


def interpret_mthfr(g_677: str, g_1298: str) -> Dict[str, str]:
    g_677 = "".join(sorted(g_677.upper()))
    g_1298 = "".join(sorted(g_1298.upper()))

    if g_677 == "TT":
        status = "677TT Homozygous (Reduced ~70%)"
        sig = "Significantly reduced methylenetetrahydrofolate reductase activity; impaired homocysteine recycling."
        rec = "Supplement with active L-methylfolate (5-MTHF 400–1000 mcg) and methylcobalamin (B12); avoid high synthetic folic acid fortification."
    elif g_677 == "CT":
        if "C" in g_1298:
            status = "Compound Heterozygous (677CT / 1298AC)"
            sig = "Moderately reduced methylation capacity (~50% efficiency); elevated plasma homocysteine risk."
            rec = "Include dietary leafy greens (natural folates), choline/betaine, and low-dose methylated B-complex."
        else:
            status = "677CT Heterozygous (Mildly Reduced ~35%)"
            sig = "Mild reduction in enzymatic velocity; usually compensated with adequate dietary folate intake."
            rec = "Ensure abundant dietary cruciferous greens and B-vitamins in diet."
    else:
        status = "677CC Wild-Type (Optimal 100%)"
        sig = "Standard 100% enzymatic methylation capacity; efficient homocysteine remethylation."
        rec = "Standard balanced nutritional intake; no special methylated folate requirement."

    return {
        "status": status,
        "clinical_significance": sig,
        "recommendation": rec
    }


def parse_raw_dna_text(content: str) -> Dict[str, str]:
    """
    Parses raw 23andMe, AncestryDNA or MyHeritage/FTDNA text to extract genotypes for known rsIDs.
    """
    found: Dict[str, str] = {}
    for line in content.splitlines():
        fields = _raw_dna_fields(line.strip())
        if fields and fields[0].lower() in CURATED_SNPS:
            found[fields[0].lower()] = fields[1] + fields[2]
    return found


def import_dna_file(
    path: str,
    father_zip: Optional[str] = None,
    mother_zip: Optional[str] = None,
    ancestry_zip: Optional[str] = None,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Imports one DNA file into the vault: a .zip (Promethease report, or AncestryDNA / 23andMe /
    MyHeritage raw data, optionally phased with parents' Promethease zips) or a raw .txt/.csv export.
    Raises ValueError when no genotypes can be read. Shared by the CLI and the web upload.
    """
    if path.lower().endswith(".zip"):
        return import_promethease_trio(path, father_zip, mother_zip, db_path=db_path, ancestry_zip=ancestry_zip)

    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        extracted = parse_raw_dna_text(f.read())
    if not extracted:
        raise ValueError(f"No recognised SNP genotypes found in {os.path.basename(path)}")
    prof = build_clinical_profile(extracted)
    target_db = db_path or vault_db_path()
    os.makedirs(os.path.dirname(os.path.abspath(target_db)), exist_ok=True)
    with open(target_db, "a", encoding="utf-8") as f:
        f.write(json.dumps(prof) + "\n")
    prof["snps_found"] = len(extracted)
    return prof


def import_promethease_trio(
    self_zip: str,
    father_zip: Optional[str] = None,
    mother_zip: Optional[str] = None,
    db_path: Optional[str] = None,
    ancestry_zip: Optional[str] = None
) -> Dict[str, Any]:
    """
    Ingests user Promethease and/or AncestryDNA raw data and phases with father and mother data.
    """
    raw_ancestry_path = ancestry_zip

    ancestry_snps: Dict[str, Dict[str, Any]] = {}
    if raw_ancestry_path and os.path.exists(raw_ancestry_path):
        try:
            ancestry_snps = extract_ancestry_snps_from_zip(raw_ancestry_path)
        except Exception as e:
            logger.warning(f"Could not extract Ancestry zip {raw_ancestry_path}: {e}")

    # Check if self_zip itself is AncestryDNA or Promethease
    p_snps: Dict[str, Dict[str, Any]] = {}
    if os.path.exists(self_zip):
        try:
            p_snps = extract_promethease_snps_from_zip(self_zip)
        except Exception:
            try:
                p_snps = extract_ancestry_snps_from_zip(self_zip)
            except Exception as e:
                logger.error(f"Failed to extract self SNPs from {self_zip}: {e}")

    if not p_snps and not ancestry_snps:
        raise ValueError(
            f"No SNPs found in {os.path.basename(self_zip)}: expected a Promethease report zip "
            "or an AncestryDNA / 23andMe / MyHeritage raw data zip"
        )

    # Merge: start with raw Ancestry calls and overlay rich Promethease SNPedia annotations
    if ancestry_snps:
        merged = dict(ancestry_snps)
        for rs, item in p_snps.items():
            if rs in merged:
                merged[rs].update(item)
            else:
                merged[rs] = item
        p_snps = merged

    father_snps = extract_promethease_snps_from_zip(father_zip) if father_zip and os.path.exists(father_zip) else {}
    mother_snps = extract_promethease_snps_from_zip(mother_zip) if mother_zip and os.path.exists(mother_zip) else {}

    def geno(snps: Dict[str, Dict[str, Any]], rs: str) -> str:
        return clean_geno(snps.get(rs, {}).get("geno"))

    user_genos = {rs: geno(p_snps, rs) for rs in CURATED_SNPS}
    profile = build_clinical_profile(user_genos)

    has_trio = bool(father_snps and mother_snps)
    trio_insights = []
    if has_trio:
        def parents(rsids: str) -> str:
            ids = rsids.split(" / ")
            father = "/".join(geno(father_snps, rs) or "N/A" for rs in ids)
            mother = "/".join(geno(mother_snps, rs) or "N/A" for rs in ids)
            return f"Father {father}, Mother {mother}"

        for v in profile["variants"]:
            v["inheritance"] = parents(v["rsid"])
        trio_insights = [
            {
                "trait": f"{info['gene']} ({rs})",
                "finding": f"{parents(rs)} -> You {user_genos[rs]}",
                "detail": info["description"],
            }
            for rs, info in CURATED_SNPS.items() if user_genos[rs]
        ]

    # Top Promethease high-magnitude findings
    top_findings = []
    notable = [it for it in p_snps.values() if float(it.get("magnitude") or 0.0) >= 2.5 and it.get("rsnum")]
    notable.sort(key=lambda x: float(x.get("magnitude") or 0.0), reverse=True)
    for it in notable[:25]:
        rs = it.get("rsnum", "").lower()
        genes = it.get("genes") or []
        top_findings.append({
            "rsid": it.get("rsnum"),
            "gene": genes[0] if genes else "",
            "genotype": it.get("geno") or "",
            "father_genotype": father_snps.get(rs, {}).get("geno") or "N/A",
            "mother_genotype": mother_snps.get(rs, {}).get("geno") or "N/A",
            "magnitude": float(it.get("magnitude") or 0.0),
            "repute": it.get("repute") or "Neutral",
            "summary": it.get("genosummary") or (it.get("genobody") or "")[:80]
        })

    profile.update({
        "source": "Raw DNA archive" + (" with parent-offspring trio" if has_trio else ""),
        "trio_status": {
            "is_phased_trio": has_trio,
            "father_analyzed": bool(father_snps),
            "mother_analyzed": bool(mother_snps),
            "user_snps_total": len(p_snps),
            "father_snps_total": len(father_snps),
            "mother_snps_total": len(mother_snps),
            "total_family_snps": len(p_snps) + len(father_snps) + len(mother_snps),
            "raw_ancestry_loaded": bool(ancestry_snps)
        },
        "trio_insights": trio_insights,
        "top_promethease_findings": top_findings,
    })

    target_db = db_path or vault_db_path()
    os.makedirs(os.path.dirname(os.path.abspath(target_db)), exist_ok=True)
    with open(target_db, "a", encoding="utf-8") as f:
        f.write(json.dumps(profile) + "\n")

    logger.info(f"Imported genomic profile ({len(p_snps):,} SNPs) into {target_db}")
    return profile


def get_genomics_profile(db_path: Optional[str] = None) -> Dict[str, Any]:
    path = db_path or vault_db_path()
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in reversed(f.readlines()):
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    if d.get("type") == "genomics":
                        return d
                except Exception:
                    pass

    return build_clinical_profile()


def build_clinical_profile(raw_genotypes: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """
    Builds a structured clinical report from the genotypes that were actually
    imported. Variants without a genotype are left out; highlights say so.
    """
    g = {rs: gt for rs, gt in (raw_genotypes or {}).items() if gt}
    missing = "Not genotyped"

    apoe_res = interpret_apoe(g.get("rs429358", ""), g.get("rs7412", ""))
    mthfr_res = interpret_mthfr(g.get("rs1801133", ""), g.get("rs1801131", ""))

    # CYP1A2
    cyp1a2_gt = "".join(sorted(g.get("rs762551", "")))
    if cyp1a2_gt == "AA":
        cyp1a2_status = "Fast / Ultra-Rapid Caffeine Metabolizer"
        cyp1a2_sig = "Rapid hepatic caffeine clearance; coffee confers measurable cardiovascular and cognitive longevity benefits without sustained hypertension."
        cyp1a2_rec = "Safe to consume 2-4 cups of polyphenol-rich coffee daily; avoid caffeine within 6 hours of bedtime."
    elif "C" in cyp1a2_gt and "A" in cyp1a2_gt:
        cyp1a2_status = "Intermediate Caffeine Metabolizer"
        cyp1a2_sig = "Moderate caffeine clearance rate; moderate doses well-tolerated."
        cyp1a2_rec = "Keep daily coffee to 1-2 cups in the morning; cease caffeine by 12:00 PM."
    else:
        cyp1a2_status = "Slow Caffeine Metabolizer"
        cyp1a2_sig = "Delayed caffeine clearance; high coffee intake can elevate systolic blood pressure and arterial stiffness."
        cyp1a2_rec = "Limit coffee to 1 cup early or switch to green tea (L-theanine synergy); cease caffeine by 10:00 AM."

    # SLCO1B1
    slco_gt = "".join(sorted(g.get("rs4149056", "")))
    if slco_gt == "TT":
        slco_status = "Normal Statin Transporter Function (SLCO1B1 *1/*1)"
        slco_sig = "Normal hepatic statin uptake; standard low baseline risk of statin-induced myopathy (muscle pain)."
        slco_rec = "Standard statin prescribing guidelines apply if cardiovascular therapy indicated; CoQ10 100mg supportive."
    elif "C" in slco_gt:
        slco_status = "Decreased Statin Clearance (SLCO1B1 *5 Carrier)"
        slco_sig = "Reduced hepatic OATP1B1 transporter function leading to elevated systemic statin circulating concentrations."
        slco_rec = "If statin therapy ever indicated, prefer rosuvastatin or pravastatin at low-to-moderate doses; avoid high-dose simvastatin; co-prescribe Ubiquinol CoQ10."
    else:
        slco_status = "Normal Statin Transporter Function (SLCO1B1 *1/*1)"
        slco_sig = "Normal hepatic statin clearance."
        slco_rec = "Standard protocol."

    # ACTN3
    actn3_gt = "".join(sorted(g.get("rs1815739", "")))
    if actn3_gt == "CC":
        actn3_status = "Power & Sprint Phenotype (ACTN3 RR)"
        actn3_sig = "High alpha-actinin-3 in fast-twitch type II fibers; rapid force generation and glycolytic power output."
        actn3_rec = "Excellent responsiveness to high-intensity interval training (HIIT), sprint intervals, and progressive resistance loading."
    elif "T" in actn3_gt and "C" in actn3_gt:
        actn3_status = "Hybrid Power & Aerobic Endurance (ACTN3 RX)"
        actn3_sig = "Balanced fast-twitch and slow-twitch muscle fiber efficiency; versatile across aerobic and resistance modalities."
        actn3_rec = "Combine Zone 2 aerobic base volume (running/cycling) with 2-3 weekly heavy resistance training sessions."
    else:
        actn3_status = "Endurance Specialist Phenotype (ACTN3 XX)"
        actn3_sig = "Complete alpha-actinin-3 deficiency; enhanced mitochondrial oxidative phosphorylation and fatigue resilience."
        actn3_rec = "Exceptional adaptation to steady-state Zone 2 endurance, distance running, and prolonged aerobic workouts."

    # FOXO3
    foxo_gt = "".join(sorted(g.get("rs2802292", "")))
    foxo_carrier = "T" in foxo_gt
    foxo_status = "FOXO3 Longevity Allele Carrier (G/T)" if foxo_carrier else "Standard FOXO3 Baseline (G/G)"
    foxo_sig = "Carries the conserved centenarian longevity allele associated with enhanced autophagic clearance and cellular resilience." if foxo_carrier else "Standard baseline autophagy regulation."
    foxo_rec = "Optimize autophagy through time-restricted feeding, regular exercise, and intermittent fasting."

    variants_report = [
        {
            "gene": "APOE",
            "rsid": "rs429358 / rs7412",
            "genotype": f"{g.get('rs429358')}/{g.get('rs7412')}",
            "status": apoe_res["isoform"],
            "category": "cardiovascular",
            "significance": apoe_res["clinical_significance"],
            "recommendation": apoe_res["recommendation"],
            "badge": "Optimal" if "ε3/ε3" in apoe_res["isoform"] or "ε2" in apoe_res["isoform"] else "Actionable"
        },
        {
            "gene": "MTHFR",
            "rsid": "rs1801133",
            "genotype": g.get("rs1801133"),
            "status": mthfr_res["status"],
            "category": "longevity",
            "significance": mthfr_res["clinical_significance"],
            "recommendation": mthfr_res["recommendation"],
            "badge": "Actionable" if "Reduced" in mthfr_res["status"] else "Optimal"
        },
        {
            "gene": "FOXO3",
            "rsid": "rs2802292",
            "genotype": g.get("rs2802292"),
            "status": foxo_status,
            "category": "longevity",
            "significance": foxo_sig,
            "recommendation": foxo_rec,
            "badge": "Longevity Boost" if foxo_carrier else "Neutral"
        },
        {
            "gene": "CYP1A2",
            "rsid": "rs762551",
            "genotype": g.get("rs762551"),
            "status": cyp1a2_status,
            "category": "metabolism",
            "significance": cyp1a2_sig,
            "recommendation": cyp1a2_rec,
            "badge": "Optimal" if "Rapid" in cyp1a2_status else "Caution"
        },
        {
            "gene": "SLCO1B1",
            "rsid": "rs4149056",
            "genotype": g.get("rs4149056"),
            "status": slco_status,
            "category": "pharmacogenomics",
            "significance": slco_sig,
            "recommendation": slco_rec,
            "badge": "Optimal" if "Normal" in slco_status else "Caution"
        },
        {
            "gene": "ACTN3",
            "rsid": "rs1815739",
            "genotype": g.get("rs1815739"),
            "status": actn3_status,
            "category": "fitness",
            "significance": actn3_sig,
            "recommendation": actn3_rec,
            "badge": "Athletic Profile"
        }
    ]

    known = {
        "APOE": "rs429358" in g and "rs7412" in g,
        "MTHFR": "rs1801133" in g,
        "FOXO3": "rs2802292" in g,
        "CYP1A2": "rs762551" in g,
        "SLCO1B1": "rs4149056" in g,
        "ACTN3": "rs1815739" in g,
    }
    variants_report = [v for v in variants_report if known[v["gene"]]]

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": "Sovereign Offline Genomic Profiler",
        "key_highlights": {
            "apoe_isoform": apoe_res["isoform"] if known["APOE"] else missing,
            "mthfr_efficiency": mthfr_res["status"] if known["MTHFR"] else missing,
            "caffeine_metabolism": cyp1a2_status if known["CYP1A2"] else missing,
            "statin_sensitivity": slco_status if known["SLCO1B1"] else missing,
            "muscle_fiber_type": actn3_status if known["ACTN3"] else missing,
            "foxo3_longevity": foxo_status if known["FOXO3"] else missing,
        },
        "variants": variants_report,
        "recommendations": [v["recommendation"] for v in variants_report],
        "type": "genomics"
    }
