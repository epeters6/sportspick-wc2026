import argparse


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run MLB outs engine in training or live decision profile."
    )
    parser.add_argument(
        "--mode",
        choices=["training", "live"],
        default="training",
        help="Decision profile: training (looser no-bet) or live (tighter no-bet). Default: training.",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    # Import after parsing so runtime mode is always explicit.
    from mlb_pitcher_fatigue_engine_v4 import engine

    engine(mode=args.mode)


if __name__ == "__main__":
    main()
