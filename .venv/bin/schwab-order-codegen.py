#!/home/phi1295/options-scanner/.venv/bin/python3
from schwab.scripts.orders_codegen import latest_order_main

if __name__ == '__main__':
    import sys
    sys.exit(latest_order_main(sys.argv[1:]))
