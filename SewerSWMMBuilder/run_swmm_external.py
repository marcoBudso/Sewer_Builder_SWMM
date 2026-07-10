import sys
from swmm.toolkit import solver


def _run_solver(inp_file, rpt_file, out_file):
    """Esegue SWMM gestendo le diverse API disponibili in swmm-toolkit."""

    if hasattr(solver, "run"):
        return solver.run(inp_file, rpt_file, out_file)

    if hasattr(solver, "swmm_run"):
        return solver.swmm_run(inp_file, rpt_file, out_file)

    available = [name for name in dir(solver) if not name.startswith("_")]
    raise AttributeError(
        "La libreria swmm-toolkit installata non contiene né solver.run né "
        "solver.swmm_run. Funzioni disponibili: " + ", ".join(available)
    )


def main():
    if len(sys.argv) != 4:
        print("Uso corretto: python run_swmm_external.py modello.inp modello.rpt modello.out")
        sys.exit(1)

    inp_file = sys.argv[1]
    rpt_file = sys.argv[2]
    out_file = sys.argv[3]

    print("Avvio simulazione SWMM...")
    print("INP:", inp_file)
    print("RPT:", rpt_file)
    print("OUT:", out_file)

    _run_solver(inp_file, rpt_file, out_file)

    print("Simulazione completata correttamente.")


if __name__ == "__main__":
    main()
