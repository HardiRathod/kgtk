"""
Test the KGTK edge file reader.
"""
from argparse import ArgumentParser
from pathlib import Path
import typing

from kgtk.join.edgereader import EdgeReader
    
def main():
    """
    Test the KGTK edge file reader.
    """
    parser = ArgumentParser()
    parser.add_argument(dest="edge_file", help="The edge file to read", type=Path, default=None)
    parser.add_argument(      "--gzip-in-parallel", dest="gzip_in_parallel", help="Execute gzip in a subthread.", action='store_true')
    parser.add_argument("-v", "--verbose", dest="verbose", help="Print additional progress messages.", action='store_true')
    parser.add_argument(      "--very-verbose", dest="very_verbose", help="Print additional progress messages.", action='store_true')
    args = parser.parse_args()

    er: EdgeReader = EdgeReader.open(args.edge_file,
                                     gzip_in_parallel=args.gzip_in_parallel,
                                     verbose=args.verbose, very_verbose=args.very_verbose)

    line_count: int = 0
    line: typing.List[str]
    for line in er:
        line_count += 1
    print("Read %d lines" % line_count)


if __name__ == "__main__":
    main()

