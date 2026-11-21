#!/usr/bin/env python3
"""
bench.py : comparer plusieurs modeles sur les memes pages avant de choisir celui de rater.py.

Les pages sont telechargees une seule fois, puis notees par chaque modele avec la meme grille.
Chaque modele est compare au consensus des AUTRES modeles (mediane de leurs notes, note globale la plus
frequente) : pas besoin de payer un gros modele pour servir d'etalon, et un modele ne se juge pas lui-meme.
--reference premier compare tout le monde au premier modele de la liste.

On mesure l'ecart a la reference, la capacite a distinguer les pages (un modele qui repond Medium partout
ne sert a rien), la stabilite d'un passage a l'autre (--repeat 2), la vitesse, les jetons et le cout reel.

  set OPENROUTER_API_KEY=sk-or-...
  python bench.py https://votre-site.fr --pages 8 --repeat 2 --max-cost 1       --models openrouter:editeur/modele-a,openrouter:editeur/modele-b,ollama:modele-c

Chaque modele s'ecrit fournisseur:nom (fournisseurs de rater.py ; la cle vient de la variable d'environnement
du fournisseur). Sorties : bench-report.md, bench-report.csv et bench-report.json (notes brutes, a fusionner
plus tard avec --merge sans refaire un seul appel).
"""
import argparse, csv, json, os, re, statistics, sys, time
from collections import Counter
from urllib.parse import urlparse

import rater


def model_cfg(spec):
	provider, _, model = spec.partition(":")
	if provider not in rater.PROVIDERS or not model:
		sys.exit(f"{spec} : ecrivez fournisseur:modele, fournisseurs {', '.join(rater.PROVIDERS)}.")
	base, _, env = rater.PROVIDERS[provider]
	key = os.environ.get(env)
	if not key:
		sys.exit(f"{spec} : cle manquante, definissez {env}.")
	return {"name": spec, "base": base, "model": model, "key": key}


SPENT = [0.0]  # cout reel cumule de tout le banc, d'apres le champ usage.cost renvoye par OpenRouter


def rate_all(cfg, pages, sites, chars, pause, repeat, max_cost):
	"""Notes de chaque page, passage par passage, sans cache (la stabilite se mesure sur de vrais appels).
	S'arrete des que le cout reel cumule du banc depasse --max-cost."""
	runs, fails, secs = [], 0, []
	for n in range(repeat):
		run = {}
		for page in pages:
			if max_cost and SPENT[0] >= max_cost:
				print(f"    plafond de {max_cost} $ atteint ({SPENT[0]:.4f} $) : {cfg['name']} arrete")
				runs.append(run)
				return runs, fails, secs
			before = cfg.get("usage", Counter())["cost"]
			t = time.time()
			try:
				user = rater.user_message(page, sites[rater.host(page["url"])], None, chars)
				run[page["url"]] = rater.coherent(rater.normalize(rater.chat(cfg, rater.GRID, user, 2500)))
				secs.append(time.time() - t)
				SPENT[0] += cfg["usage"]["cost"] - before
			except Exception as e:
				fails += 1
				print(f"    echec {page['url']} : {str(e)[:120]}")
			time.sleep(pause)
		runs.append(run)
		print(f"  {cfg['name']} passage {n + 1}/{repeat} : {len(run)}/{len(pages)} pages")
	return runs, fails, secs


def mae(a, b):
	return statistics.mean(abs(a["scores"][k] - b["scores"][k]) for k in rater.SCORE_KEYS)


def main():
	ap = argparse.ArgumentParser(description="Comparer des modeles pour rater.py sur les memes pages.")
	ap.add_argument("url", nargs="*")
	ap.add_argument("--models", help="fournisseur:modele separes par des virgules")
	ap.add_argument("--merge", help="fichiers .json de bancs precedents (separes par des virgules) a comparer ensemble, sans nouvel appel")
	ap.add_argument("--reference", choices=["consensus", "premier"], default="consensus",
		help="etalon : mediane des modeles testes (defaut) ou premier modele de la liste")
	ap.add_argument("--pages", type=int, default=8)
	ap.add_argument("--one", action="store_true", help="noter seulement les URL donnees, sans crawl")
	ap.add_argument("--repeat", type=int, default=1, help="passages par modele (2 pour mesurer la stabilite)")
	ap.add_argument("--chars", type=int, default=12000)
	ap.add_argument("--pause", type=float, default=2)
	ap.add_argument("--max-cost", type=float, default=0, help="plafond en $ du cout reel cumule (OpenRouter), 0 = sans plafond")
	ap.add_argument("--out", default="bench-report")
	a = ap.parse_args()
	if a.merge:
		data = {"pages": [], "sites": [], "repeat": a.repeat, "results": {}}
		for path in a.merge.split(","):
			with open(path.strip(), encoding="utf-8") as f:
				d = json.load(f)
			data["pages"] += [u for u in d["pages"] if u not in data["pages"]]
			data["sites"] += [h for h in d["sites"] if h not in data["sites"]]
			data["repeat"] = d["repeat"]
			data["results"].update(d["results"])
		return report(data, a.reference, a.out)
	if not a.models or not a.url:
		sys.exit("Donnez les adresses et --models (ou --merge).")
	cfgs = [model_cfg(m.strip()) for m in a.models.split(",") if m.strip()]

	urls = [u if u.startswith("http") else "https://" + u for u in a.url]
	if a.one:
		pages = []
		for u in urls:
			try:
				res = rater.fetch(u)
				pages.append(rater.parse(rater.clean(res["url"]), res["text"], res["x_robots"]))
			except Exception as e:
				print(f"  ! {u} : {e}")
	else:
		pages = rater.crawl(urls[0], a.pages, re.compile(rater.DEFAULT_EXCLUDE, re.I))
	if not pages:
		sys.exit("Aucune page exploitable.")
	# transparence de chaque site present (le banc peut melanger plusieurs sites avec --one)
	sites = {}
	for p in pages:
		h = rater.host(p["url"])
		if h not in sites:
			pu = urlparse(p["url"])
			sites[h] = rater.site_signals(f"{pu.scheme}://{pu.netloc}/")

	results = {}
	for cfg in cfgs:
		print(f"\n{cfg['name']} ...")
		runs, fails, secs = rate_all(cfg, pages, sites, a.chars, a.pause, a.repeat, a.max_cost)
		results[cfg["name"]] = {"runs": runs, "fails": fails, "secs": secs, "usage": dict(cfg.get("usage", Counter()))}
		data = {"pages": [p["url"] for p in pages], "sites": list(sites), "repeat": a.repeat, "results": results}
		with open(a.out + ".json", "w", encoding="utf-8") as f:  # resultats bruts, enregistres apres chaque modele
			json.dump(data, f, ensure_ascii=False)
	report(data, a.reference, a.out)


def report(data, reference, out):
	"""Tableau comparatif d'un banc (ou de plusieurs bancs fusionnes) : CSV et Markdown."""
	pages, results = data["pages"], data["results"]

	def consensus(without=None):
		"""Mediane des notes et note globale la plus frequente, page par page, sans le modele juge."""
		ref = {}
		for p in pages:
			votes = [results[n]["runs"][0][p] for n in results if n != without and p in results[n]["runs"][0]]
			if len(votes) >= 2:
				ref[p] = {"scores": {k: statistics.median(v["scores"][k] for v in votes) for k in rater.SCORE_KEYS},
					"pq_rating": Counter(v["pq_rating"] for v in votes).most_common(1)[0][0]}
		return ref
	ref_name = next(iter(results)) if reference == "premier" else "consensus"
	rows = []
	for name, r in results.items():
		runs, secs, usage = r["runs"], r["secs"], Counter(r["usage"])
		first = runs[0]
		ref = results[ref_name]["runs"][0] if reference == "premier" else consensus(without=name)
		common = [p for p in first if p in ref]
		ratings = [first[p]["pq_rating"] for p in first]
		spread = statistics.mean(statistics.pstdev([first[p]["scores"][k] for p in first]) for k in rater.SCORE_KEYS) if len(first) > 1 else 0
		stab = [mae(runs[0][p], runs[1][p]) for p in runs[0] if len(runs) > 1 and p in runs[1]]
		done = sum(len(r) for r in runs)
		rows.append({
			"modele": name,
			"pages notees": f"{done}/{len(pages) * len(runs)}",
			"ecart a la reference (/10)": "-" if name == ref_name else f"{statistics.mean(mae(first[p], ref[p]) for p in common):.2f}" if common else "?",
			"meme note globale": "-" if name == ref_name else f"{sum(first[p]['pq_rating'] == ref[p]['pq_rating'] for p in common)}/{len(common)}",
			"dispersion des notes": f"{spread:.2f}",
			"part de Medium": f"{ratings.count('Medium') / len(ratings):.0%}" if ratings else "?",
			"instabilite (/10)": f"{statistics.mean(stab):.2f}" if stab else "-",
			"secondes par page": f"{statistics.median(secs):.1f}" if secs else "?",
			"jetons par page": f"{(usage['prompt_tokens'] + usage['completion_tokens']) / max(usage['calls'], 1):.0f}",
			"cout reel pour 1000 pages": f"{usage['cost'] / max(done, 1) * 1000:.2f} $" if usage["cost"] else "non facture (forfait)",
		})
	keys = list(rows[0])
	with open(out + ".csv", "w", newline="", encoding="utf-8-sig") as f:
		w = csv.DictWriter(f, keys, delimiter=";")
		w.writeheader()
		w.writerows(rows)
	lines = [f"# Banc d'essai rater.py", "", f"{len(pages)} pages de {', '.join(data['sites'])}, {data['repeat']} passage(s) par modele, "
		f"reference : {'consensus des autres modeles' if ref_name == 'consensus' else ref_name}.", "", "| " + " | ".join(keys) + " |", "|" + "---|" * len(keys)]
	lines += ["| " + " | ".join(str(r[k]) for k in keys) + " |" for r in rows]
	cols = list(results) + (["consensus"] if ref_name == "consensus" else [])
	everyone = consensus()
	lines += ["", "## Notes globales par page", "", "| page | " + " | ".join(cols) + " |", "|" + "---|" * (len(cols) + 1)]
	for p in pages:
		cells = [results[n]["runs"][0].get(p, {}).get("pq_rating", "echec") for n in results]
		if ref_name == "consensus":
			cells.append(everyone.get(p, {}).get("pq_rating", "?"))
		lines.append(f"| {p} | " + " | ".join(cells) + " |")
	lines += ["", "Lecture : un bon modele s'ecarte peu de la reference, disperse ses notes (il distingue les pages),"
		" reste stable d'un passage a l'autre et ne repond pas Medium partout."]
	with open(out + ".md", "w", encoding="utf-8") as f:
		f.write("\n".join(lines) + "\n")
	print("\n" + "\n".join(lines))


if __name__ == "__main__":
	main()
