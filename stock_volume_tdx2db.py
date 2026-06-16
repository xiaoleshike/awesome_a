import sys

from stock_volume_compare import main


if __name__ == "__main__":
    main(["--provider", "tdx2db", *sys.argv[1:]])
