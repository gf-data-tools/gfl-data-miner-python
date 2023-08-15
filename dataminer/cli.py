import argparse
import os

from logger_tt import logger, setup_logging

from .data_miner import DataMiner, GithubEnv


def cli():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "region", nargs="+", choices=["ch", "tw", "kr", "us", "jp", "at"]
    )
    parser.add_argument("--force", "-f", action="store_true")
    parser.add_argument(
        "--loglevel",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
    )
    parser.add_argument("--author", type=str, default="")
    parser.add_argument("--github_token", type=str, default="")
    parser.add_argument("--dingtalk_token", type=str, default="")
    parser.add_argument("--qq_channel", type=str, default="")
    parser.add_argument("--qq_token", type=str, default="")

    args = parser.parse_args()

    logger_cfg = setup_logging(log_path=os.devnull)
    for hdlr in logger_cfg.root_handlers:
        hdlr.setLevel(args.loglevel)

    error = False
    for region in args.region:
        try:
            print(f"::group::{region.upper()} Server")
            data_miner = DataMiner(
                region=region,
                data_dir=f"data/{region}",
                github_repo=f"gf-data-tools/gf-data-{region}",
                github_token=args.github_token,
                git_author="ZeroRin <ZeroRin@users.noreply.github.com>",
                dingtalk_token=args.dingtalk_token,
                qq_channel=args.qq_channel,
                qq_token=args.qq_token,
            )
            if args.force or data_miner.update_available():
                data_miner.repo
                data_miner.clear_local_data()
                data_miner.download_asset_bundles()
                data_miner.unpack_assets()
                data_miner.download_stc()
                data_miner.process_stc()
                data_miner.process_catchdata()
                data_miner.format_hjson()
                if data_miner.commit_repo(push=True):
                    GithubEnv()["update_detected"] = "true"
            print("::endgroup::")
        except Exception as e:
            logger.exception(repr(e))
            error = True
    if error:
        raise RuntimeError("Error during execution")
