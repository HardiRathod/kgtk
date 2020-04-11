"""
Read a KGTK edge file in TXV format.

TODO: Add support for decompression and alternative envelope formats,
such as JSON.
"""

import attr
import gzip
from pathlib import Path
from multiprocessing import Queue
import sys
from multiprocessing import Process
import typing

# This helper class supports running gzip in parallel.
#
# TODO: can we use attrs here?
class GunzipProcess(Process):
    gzip_file: typing.TextIO = attr.ib() # Todo: validate TextIO

    # The line queue contains str with None as a plug.
    #
    # TODO: can we do a better job of type declaration here?
    line_queue: Queue = attr.ib(validator=attr.validators.instance_of(Queue))

    def __init__(self,  gzip_file: typing.TextIO, line_queue: Queue):
        super().__init__()
        self.gzip_file = gzip_file
        self.line_queue = line_queue

    def run(self):
        line: str
        for line in self.gzip_file:
            self.line_queue.put(line)
        self.line_queue.put(None) # Plug the queue.

    # This is an iterator object.
    def __iter__(self)-> typing.Iterator:
        return self
    
    def __next__(self)->str:
        line: typing.Optional[str] = self.line_queue.get()
        if line is None: # Have we reached the plug?
            raise StopIteration
        else:
            return line

@attr.s(slots=True, frozen=True)
class EdgeReader:
    file_path: typing.Optional[Path] = attr.ib(validator=attr.validators.optional(attr.validators.instance_of(Path)))
    file_in: typing.TextIO = attr.ib() # Todo: validate TextIO
    column_separator: str = attr.ib(validator=attr.validators.instance_of(str))
    column_names: typing.List[str] = attr.ib(validator=attr.validators.deep_iterable(member_validator=attr.validators.instance_of(str),
                                                                                     iterable_validator=attr.validators.instance_of(list)))
    column_name_map: typing.Mapping[str, int] = attr.ib(validator=attr.validators.deep_mapping(key_validator=attr.validators.instance_of(str),
                                                                                               value_validator=attr.validators.instance_of(int)))

    # For convenience, the count of columns. This is the same as len(column_names).
    column_count: int = attr.ib(validator=attr.validators.instance_of(int))

    # The indices of the three mandatory columns:
    node1_column_idx: int = attr.ib(validator=attr.validators.instance_of(int))
    node2_column_idx: int = attr.ib(validator=attr.validators.instance_of(int))
    label_column_idx: int = attr.ib(validator=attr.validators.instance_of(int))

    # Require or fill trailing fields?
    require_all_columns: bool = attr.ib(validator=attr.validators.instance_of(bool))
    prohibit_extra_columns: bool = attr.ib(validator=attr.validators.instance_of(bool))
    fill_missing_columns: bool = attr.ib(validator=attr.validators.instance_of(bool))

    # Other implementation options?
    gzip_in_parallel: bool = attr.ib(validator=attr.validators.instance_of(bool))
    gzip_thread: typing.Optional[GunzipProcess] = attr.ib(validator=attr.validators.optional(attr.validators.instance_of(GunzipProcess)))
    gzip_queue_size: int = attr.ib(validator=attr.validators.instance_of(int))

    # When we report line numbers in error messages, line 1 is the first line after the header line.
    #
    # The use of a list is a sneaky way to get around the frozen class.
    # TODO: Find the right way to do this.  Don't freeze the class?
    line_count: typing.List[int] = attr.ib(validator=attr.validators.deep_iterable(member_validator=attr.validators.instance_of(int),
                                                                                   iterable_validator=attr.validators.instance_of(list)))

    verbose: bool = attr.ib(validator=attr.validators.instance_of(bool))
    very_verbose: bool = attr.ib(validator=attr.validators.instance_of(bool))


    # TODO: There must be some place to import these constants
    NODE1_COLUMN_NAME: str = "node1"
    NODE2_COLUMN_NAME: str = "node2"
    LABEL_COLUMN_NAME: str = "label"

    GZIP_QUEUE_SIZE_DEFAULT: int = 1000

    @classmethod
    def open(cls,
             file_path: typing.Optional[Path],
             require_all_columns: bool = True,
             prohibit_extra_columns: bool = True,
             fill_missing_columns: bool = False,
             gzip_in_parallel: bool = False,
             gzip_queue_size: int = GZIP_QUEUE_SIZE_DEFAULT,
             column_separator: str = "\t",
             verbose: bool = False,
             very_verbose: bool = False)->"EdgeReader":
        if file_path is None or str(file_path) == "-":
            if verbose:
                print("EdgeReader: reading stdin")
            return cls._setup(file_path=None,
                              file_in=sys.stdin,
                              require_all_columns=require_all_columns,
                              prohibit_extra_columns=prohibit_extra_columns,
                              fill_missing_columns=fill_missing_columns,
                              gzip_in_parallel=gzip_in_parallel,
                              gzip_queue_size=gzip_queue_size,
                              column_separator=column_separator,
                              verbose=verbose,
                              very_verbose=very_verbose,
            )
        
        if verbose:
            print("File_path.suffix: %s" % file_path.suffix)
        if file_path.suffix == ".gz":
            if verbose:
                print("EdgeReader: reading gzip %s" % str(file_path))

            # TODO: find a better way to coerce typing.IO[Any] to typing.TextIO
            gzip_file: typing.TextIO = gzip.open(file_path, mode="rt") # type: ignore
            return cls._setup(file_path=file_path,
                              file_in=gzip_file,
                              require_all_columns=require_all_columns,
                              prohibit_extra_columns=prohibit_extra_columns,
                              fill_missing_columns=fill_missing_columns,
                              gzip_in_parallel=gzip_in_parallel,
                              gzip_queue_size=gzip_queue_size,
                              column_separator=column_separator,
                              verbose=verbose,
                              very_verbose=very_verbose,
            )
            
        else:
            if verbose:
                print("EdgeReader: reading file %s" % str(file_path))
            return cls._setup(file_path=file_path,
                              file_in=open(file_path, "r"),
                              require_all_columns=require_all_columns,
                              prohibit_extra_columns=prohibit_extra_columns,
                              fill_missing_columns=fill_missing_columns,
                              gzip_in_parallel=gzip_in_parallel,
                              gzip_queue_size=gzip_queue_size,
                              column_separator=column_separator,
                              verbose=verbose,
                              very_verbose=very_verbose,
)
    
    @classmethod
    def _setup(cls,
               file_path: typing.Optional[Path],
               file_in: typing.TextIO,
               require_all_columns: bool,
               prohibit_extra_columns: bool,
               fill_missing_columns: bool,
               gzip_in_parallel: bool,
               gzip_queue_size: int,
               column_separator: str,
               verbose: bool = False,
               very_verbose: bool = False,
    )->"EdgeReader":
        """
        Read the edge file header and split it into column names. Locate the three essential comumns.
        """
        # Read the column names from the first line.
        header: str = file_in.readline()
        if verbose:
            print("header: %s" % header)
        #
        # TODO: if the read fails, throw a useful exception.

        # Split the first line into column names.
        column_names: typing.List[str] = header.split(column_separator)
        if len(column_names) < 3:
            # TODO: throw a better exception
            raise ValueError("The edge file header must have at least three columns.")

        # Validate the column names and build a map from column name
        # to column index.
        column_name_map: typing.MutableMapping[str, int] = { }
        column_idx: int = 0 # There may be a more pythonic way to do this
        column_name: str
        for column_name in column_names:
            if column_name is None or len(column_name) == 0:
                # TODO: throw a better exception
                raise ValueError("Invalid column name in the edge file header")
            column_name_map[column_name] = column_idx
            column_idx += 1

        if EdgeReader.NODE1_COLUMN_NAME not in column_name_map:
            # TODO: throw a better exception
            raise ValueError("Missing node1 column in the edge file header")
        else:
            node1_column_idx: int = column_name_map[EdgeReader.NODE1_COLUMN_NAME]

        if EdgeReader.NODE2_COLUMN_NAME not in column_name_map:
            # TODO: throw a better exception
            raise ValueError("Missing node2 column in the edge file header")
        else:
            node2_column_idx: int = column_name_map[EdgeReader.NODE2_COLUMN_NAME]

        if EdgeReader.LABEL_COLUMN_NAME not in column_name_map:
            # TODO: throw a better exception
            raise ValueError("Missing label column in the edge file header")
        else:
            label_column_idx: int = column_name_map[EdgeReader.LABEL_COLUMN_NAME]

        gzip_thread: typing.Optional[GunzipProcess] = None
        if gzip_in_parallel:
            gzip_thread = GunzipProcess(file_in, Queue(gzip_queue_size))
            gzip_thread.start()

        return cls(file_path=file_path,
                   file_in=file_in,
                   column_separator=column_separator,
                   column_names=column_names,
                   column_name_map=column_name_map,
                   column_count=len(column_names),
                   node1_column_idx=node1_column_idx,
                   node2_column_idx=node2_column_idx,
                   label_column_idx=label_column_idx,
                   require_all_columns=require_all_columns,
                   prohibit_extra_columns=prohibit_extra_columns,
                   fill_missing_columns=fill_missing_columns,
                   gzip_in_parallel=gzip_in_parallel,
                   gzip_thread=gzip_thread,
                   gzip_queue_size=gzip_queue_size,
                   line_count=[1], # TODO: find a better way to do this.
                   verbose=verbose,
                   very_verbose=very_verbose,
        )

    # This is an iterator object.
    def __iter__(self)-> typing.Iterator:
        return self

    # Get the next edge values as a list of strings.
    # TODO: Convert integers, coordinates, etc. to Python types
    def __next__(self)-> typing.List[str]:
        line: str
        try:
            if self.gzip_thread is not None:
                line = next(self.gzip_thread) # TODO: unify this
            else:
                line = next(self.file_in) # Will throw StopIteration
        except StopIteration as e:
            # Close the input file!
            #
            # TODO: implement a close() routine and/or whatever it takes to support "with".
            self.file_in.close() # Do we need to guard against repeating this call?
            raise e

        values: typing.List[str] = line.split(self.column_separator)

        # Optionally validate that the line contained the right number of columns:
        #
        # When we report line numbers in error messages, line 1 is the first line after the header line.
        if self.require_all_columns and len(values) < self.column_count:
            raise ValueError("Required %d columns in input line %d, saw %d: '%s'" % (self.column_count, self.line_count[0], len(values), line))
        if self.prohibit_extra_columns and len(values) > self.column_count:
            raise ValueError("Required %d columns in input line %d, saw %d (%d extra): '%s'" % (self.column_count, self.line_count[0], len(values),
                                                                                                len(values) - self.column_count, line))

        # Optionally fill missing trailing columns with empty values:
        if self.fill_missing_columns and len(values) < self.column_count:
            while len(values) < self.column_count:
                values.append("")

        self.line_count[0] += 1
        if self.very_verbose:
            sys.stdout.write(".")
            sys.stdout.flush()
            
        return values
