"""Print retrieval-quality metrics for the labelled test set (Step 26).

    python manage.py retrieval_quality
    python manage.py retrieval_quality --k 5 --report eval_report.txt

Prints precision@1 / recall@3 / recall@5 / MRR overall and by framework and
language. With --report, also writes a UTF-8 file listing every miss (the
French query + what was retrieved instead) for diagnosis. Console output is
ASCII-only so it's safe on a cp1252 Windows terminal.
"""

from django.core.management.base import BaseCommand

from knowledge.retrieval import retrieve
from knowledge.retrieval_eval import TEST_SET, evaluate, _rank_of


class Command(BaseCommand):
    help = "Measure retrieval precision against the labelled test set."

    def add_arguments(self, parser):
        parser.add_argument("--k", type=int, default=5)
        parser.add_argument("--report", default=None,
                            help="Write a UTF-8 miss report to this path.")

    def handle(self, *args, **options):
        k = options["k"]
        metrics = evaluate(k=k)

        order = ["overall", "fw:SYSCOHADA-2017", "fw:CGI-2025",
                 "lang:fr", "lang:en"]
        self.stdout.write(f"Retrieval quality (top-k={k}) "
                          f"— {len(TEST_SET)} labelled queries")
        self.stdout.write("-" * 64)
        self.stdout.write(f"{'bucket':<22}{'n':>4} {'P@1':>7} "
                          f"{'R@3':>7} {'R@5':>7} {'MRR':>7}")
        for name in order:
            m = metrics.get(name)
            if not m:
                continue
            self.stdout.write(
                f"{name:<22}{m['n']:>4} {m['p_at_1']:>7.1%} "
                f"{m['r_at_3']:>7.1%} {m['r_at_5']:>7.1%} {m['mrr']:>7.2f}")

        # Console-safe miss list (ASCII slugs only).
        misses = [(q, exp, fw, lang) for (q, exp, fw, lang) in TEST_SET
                  if _rank_of(exp, retrieve(q, framework=fw, k=k,
                                            only_effective=False)) is None]
        self.stdout.write("-" * 64)
        self.stdout.write(f"misses (expected not in top-{k}): {len(misses)}")
        for _, exp, fw, lang in misses:
            self.stdout.write(f"  - {exp}  [{lang}]")

        if options["report"]:
            self._write_report(options["report"], k, misses)
            self.stdout.write(f"wrote miss report -> {options['report']}")

    def _write_report(self, path, k, misses):
        lines = [f"Retrieval miss report (top-{k}) — {len(misses)} misses\n"]
        for q, exp, fw, lang in misses:
            got = retrieve(q, framework=fw, k=k, only_effective=False)
            got_slugs = ", ".join(r["slug"] for r in got[:3]) or "(none)"
            lines.append(f"[{lang}] query: {q}")
            lines.append(f"      expected: {exp}")
            lines.append(f"      top-3 got: {got_slugs}\n")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
