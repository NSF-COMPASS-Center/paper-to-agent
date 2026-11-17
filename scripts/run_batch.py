"""Convert a list of PDFs one after another and print each result line.

    python scripts/run_batch.py uv     # the 7 UV papers chosen for the second test set
    python scripts/run_batch.py ls     # the 7 loss_survival papers
    python scripts/run_batch.py redo   # paper 15 fresh, plus table pages of 3, 8, 17, 54

The paper lists are fixed (random.seed(20261101) over papers with no conversion in
p2agent_runs), so reruns are comparable. Logs go to logs/<name>.log.
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNS = Path("/Users/ayush/Desktop/nsf_compass_repos/p2agent_runs")
PY = str(ROOT / ".venv" / "bin" / "python")

UV = ["Ding et al. - 2017 - Assessing UV Inactivation of Adenovirus 41 Using I",
      "Gunter‐Ward et al. - 2018 - Efficacy of ultraviolet (UV‐C) light in reducing f",
      "Werbin et al. - 1967 - PHOTOBIOLOGY OF RNA BACTERIOPHAGES—I. ULTRAVIOLET ",
      "Calgua et al. - 2014 - UVC Inactivation of dsDNA and ssRNA Viruses in Wat",
      "Bowker et al. - 2011 - Microbial UV fluence-response assessment using a n",
      "Braunstein et al. - 1996 - Ultraviolet disinfection of filtered activated slu",
      "Mattle 그리고 Kohn - 2012 - Inactivation and Tailing during UV 254 "]
LS = ["HaddrellCO2Aerostability2024", "HinshawMMSLeukemiaVirusSurival1980",
      "Efstratiou_Crystalline", "ElazharyBovineAdenovirus1979", "Kormuth2019strains",
      "Mehrgardi2022H2O2", "SongerRHAIrborneViruses1967"]


def slug(stem: str, prefix: str) -> str:
    # "Ding et al. - 2017 - Assessing..." -> uv-ding-2017 ; "Haddrell...2024" -> ls-haddrellco2aerostability2024
    m = re.match(r"([A-Za-z‐\-\u0080-￿]+?)(?: et al\.| \S+ Kohn)? - (\d{4})", stem)
    base = f"{m[1]}-{m[2]}" if m else stem
    base = re.sub(r"[^a-z0-9]+", "-", base.lower().replace("‐", "-")).strip("-")
    return f"{prefix}-{base}"[:55].rstrip("-")


def run(name: str, args: list[str]) -> None:
    log = ROOT / "logs" / f"{name}.log"
    log.parent.mkdir(exist_ok=True)
    with log.open("w") as fh:
        rc = subprocess.run([PY, "run.py", "convert", *args], cwd=ROOT, stdout=fh,
                            stderr=subprocess.STDOUT).returncode
    tail = [l for l in log.read_text().splitlines() if l.startswith(("status", "note"))]
    print(f"{name}: exit {rc} {' | '.join(tail)}", flush=True)


def fresh(folder: str, stem: str, prefix: str) -> None:
    name = slug(stem, prefix)
    run(name, [str(RUNS / folder / "papers" / f"{stem}.pdf"), "--name", name,
               "--out", f"dist/{name}", "--work", f"runs/{name}-review"])


def main() -> None:
    which = sys.argv[1]
    if which == "uv":
        for s in UV:
            fresh("uv_extractions", s, "uv")
    elif which == "ls":
        for s in LS:
            fresh("ls_extractions", s, "ls")
    elif which == "redo":
        cfs = RUNS / "cfs_extractions" / "papers"
        p15 = next(cfs.glob("15 *.pdf"))
        run("paper-15-v2", [str(p15), "--name", "paper-15", "--out", "dist/paper-15",
                            "--work", "runs/paper-15-review-v2"])
        for n, pages in ((8, "4,6,7"), (17, "7"), (54, "5"), (3, "18,19,20,22,23,24,26")):
            pdf = next(cfs.glob(f"{n} *.pdf"))
            run(f"paper-{n}-tables", [str(pdf), "--name", f"paper-{n}", "--out", f"dist/paper-{n}",
                                      "--work", f"runs/paper-{n}-review", "--resume",
                                      "--pages", pages, "--force"])


main()
