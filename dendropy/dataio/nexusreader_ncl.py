#! /usr/bin/env python

###############################################################################
##  DendroPy Phylogenetic Computing Library.
##
##  Copyright 2009 Jeet Sukumaran and Mark T. Holder.
##
##  This program is free software; you can redistribute it and/or modify
##  it under the terms of the GNU General Public License as published by
##  the Free Software Foundation; either version 3 of the License, or
##  (at your option) any later version.
##
##  This program is distributed in the hope that it will be useful,
##  but WITHOUT ANY WARRANTY; without even the implied warranty of
##  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
##  GNU General Public License for more details.
##
##  You should have received a copy of the GNU General Public License along
##  with this program. If not, see <http://www.gnu.org/licenses/>.
##
###############################################################################

"""
Facultative use of NCL for NEXUS parsing.
"""

import os
from dendropy.utility.messaging import get_logger
_LOG = get_logger("dataio.ncl")

DENDROPY_NCL_AVAILABILITY = False
try:
    from nexusclasslib import nclwrapper
    DENDROPY_NCL_AVAILABILITY = True
except ImportError:
    DENDROPY_NCL_AVAILABILITY = False
else:

    import os
    from threading import Thread, Event
    from dendropy import dataobject
    from dendropy.dataio import nexusreader_py
    from dendropy.dataio import nexustokenizer
    from dendropy.utility import iosys

    if "DENDROPY_ENABLE_NCL_WARNINGS" in os.environ:
        DENDROPY_NCL_WARNING_LEVEL = nclwrapper.NxsReader.SKIPPING_CONTENT_WARNING
    else:
        DENDROPY_NCL_WARNING_LEVEL = nclwrapper.NxsReader.SUPPRESS_WARNINGS_LEVEL

    def _ncl_datatype_enum_to_dendropy(d):
        e = nclwrapper.NxsCharactersBlock
        if d == e.dna:
            return dataobject.DnaCharacterMatrix
        if d == e.nucleotide:
            return dataobject.NucleotideCharacterMatrix
        if d == e.rna:
            return dataobject.RnaCharacterMatrixk
        if d == e.protein:
            return dataobject.ProteinCharacterMatrix
        if (d == e.continuous):
            return dataobject.ContinuousCharacterMatrix
        if d == e.standard:
            return dataobject.StandardCharacterMatrix
        if (d == e.mixed) or (d == e.codon):
            s = d == e.continuous and "continuous" or (d == e.mixed and "mixed" or "codon")
            raise NotImplementedError("%s datatype not supported" % s)
        print d
        print dict(e)
        sys.exit(0)

    class NCLTreeStream(nclwrapper.NxsTreeStream):
        """Simple thread-safe class that waits for `need_tree_event', and signals the
        presence of a new tree by `ready_event`"""
        def __init__(self, need_tree_event, ready_event, die_event):
            self.need_tree_event = need_tree_event
            self.ready_event = ready_event
            self.tree_tokens = None
            self.ncl_taxa_block =  None
            self.exception = None
            self.die_event = die_event
            nclwrapper.NxsTreeStream.__init__(self)

        def handleTree(self, ftd, tb):
            t = ftd.GetTreeTokens()
            rooted_flag = ftd.IsRooted()
            if self.die_event.isSet():
                raise RuntimeError("exception in calling thread")
            self.need_tree_event.wait()
            self.need_tree_event.clear()
            if self.die_event.isSet():
                raise RuntimeError("exception in calling thread")
            try:
                self.ncl_taxa_block =  tb.GetTaxaBlockPtr()
                tb_iid = self.ncl_taxa_block.GetInstanceIdentifierString()
                self.tree_tokens =  t
                self.rooted_flag = rooted_flag
            except Exception, v:
                _LOG.debug("NCLTreeStream Exception: %s" % str(v))
                self.exception = v
                self.ncl_taxa_block = None
                self.tree_tokens = None
                self.rooted_flag = None
                self.ready_event.set()
                raise v

            self.ready_event.set()
            return False

    class NCLTreeStreamThread(Thread):
        def __init__(self, file_path, need_tree_event, ready_event, die_event, format="NEXUS", **kwargs):
            """Subclass of thread ,that uses a NCLTreeStream to get trees
            from NCL one at a time"""
            self.nts = NCLTreeStream(need_tree_event, ready_event, die_event)
            self.file_path = file_path
            self.format = format
            self.exception = None
            self.done = False
            self.reader = nclwrapper.MultiFormatReader()
            #self.reader.SetWarningOutputLevel(DENDROPY_NCL_WARNING_LEVEL)
            self.reader.cullIdenticalTaxaBlocks(True)
            self.die_event = die_event
            Thread.__init__(self,
                            group=None,
                            target=None,
                            name=None,
                            args=tuple(),
                            kwargs=dict(**kwargs))
        def run(self):
            self.done = False
            self.exception = None
            try:
                if not self.die_event.isSet():
                    self.nts.ReadFilepath(self.file_path, self.format, self.reader)
            except Exception, v:
                if self.nts.exception:
                    self.exception = self.nts.exception
                else:
                    self.exception = v
                _LOG.debug("NCLTreeStreamThread Exception: %s" % str(self.exception))
            else:
                self.nts.need_tree_event.wait()
                self.nts.need_tree_event.clear()
            self.done = True
            self.nts.tree_tokens = None
            self.nts.taxa_block = None
            self.rooted_flag = None
            self.nts.ready_event.set()

    class ListOfTokenIterator(object):
        def __init__(self, tokens):
            self.tokens_iter = iter(tokens)
            self.eof = False
            self.queued = None
            self.tree_rooted = None
            self.comments = None

        def clear_comments(self):
            self.tree_rooted = None
            self.comments = None

        def tree_rooted_comment(self):
            "This is a hack, and only works if you have just one tree in the token stream"
            return self.tree_rooted

        def __iter__(self):
            return self
        def read_next_token(self, ignore_punctuation=None):
            if not self.eof:
                try:
                    return self.tokens_iter.next()
                except StopIteration:
                    self.eof = True
            return ""

        def read_next_token_ucase(self):
            t = self.read_next_token()
            if t:
                return t.upper()
        def syntax_exception(self, msg):
            return SyntaxException(message=msg)

    class NCLBasedReader(iosys.DataReader):
        "Encapsulates loading and parsing of a NEXUS format file."

        def __init__(self, schema="NEXUS", **kwargs):
            iosys.DataReader.__init__(self)
            self.purePythonReader = nexusreader_py.NexusReader(**kwargs)
            self.encode_splits = kwargs.get("encode_splits", False)
            self.rooting_interpreter = kwargs.get("rooting_interpreter", nexustokenizer.RootingInterpreter(**kwargs))
            self.finish_node_func = kwargs.get("finish_node_func", None)
            self.allow_duplicate_taxon_labels = kwargs.get("allow_duplicate_taxon_labels", False)
            self.preserve_underscores = kwargs.get('preserve_underscores', False)
            self.suppress_internal_node_taxa = kwargs.get("suppress_internal_node_taxa", False)
            self.finish_node_func = None
            self.format = schema
            self._prev_taxa_block = None
            self.ncl_taxa_to_native = {}
            self._taxa_to_fill = None

        def _get_fp(self, file_obj):
            "Returns filepath and True if the file that `file_obj` refers to exists on the filesystem"
            try:
                n = file_obj.name
                use_ncl = os.path.exists(n)
                return n, use_ncl
            except AttributeError:
                return "", False

        def read(self, stream):
            """
            Instantiates and returns a DataSet object based on the
            NEXUS-formatted contents read from the file descriptor object
            `file_obj`.
            """
            n, use_ncl = self._get_fp(stream)
            if not use_ncl:
                self.purePythonReader.encode_splits = self.encode_splits
                self.purePythonReader.rooting_interpreter = self.rooting_interpreter
                self.purePythonReader.finish_node_func = self.finish_node_func
                self.purePythonReader.allow_duplicate_taxon_labels = self.allow_duplicate_taxon_labels
                self.purePythonReader.preserve_underscores = self.preserve_underscores
                self.purePythonReader.suppress_internal_node_taxa = self.suppress_internal_node_taxa
                return self.purePythonReader.read(stream)
            return self.read_filepath_into_dataset(n)

        def read_filepath_into_dataset(self, file_path):
            if self.dataset is None:
                self.dataset = dataobject.DataSet()
            self._taxa_to_fill = None
            _LOG.debug("Creating MultiFormatReader")
            ncl_nxs_reader_handle = nclwrapper.MultiFormatReader()
            _LOG.debug("Setting MultiFormatReader's WarningOutput Level")
            ncl_nxs_reader_handle.SetWarningOutputLevel(DENDROPY_NCL_WARNING_LEVEL)
            _LOG.debug("Calling MultiFormatReader.cullIdenticalTaxaBlocks(True)")
            ncl_nxs_reader_handle.cullIdenticalTaxaBlocks(True)

            self._register_taxa_context(ncl_nxs_reader_handle, self.dataset.taxon_sets)
            _LOG.debug("Calling MultiFormatReader.ReadFilepath(%s, %s)" % (file_path, self.format))
            ncl_nxs_reader_handle.ReadFilepath(file_path, self.format)

            _LOG.debug("Calling MultiFormatReader.GetNumTaxaBlocks()")
            num_taxa_blocks = ncl_nxs_reader_handle.GetNumTaxaBlocks()
            for i in xrange(num_taxa_blocks):
                _LOG.debug("Calling MultiFormatReader.GetTaxaBlock(%d)" % i)
                ncl_tb = ncl_nxs_reader_handle.GetTaxaBlock(i)
                taxa_block = self._ncl_taxa_block_to_native(ncl_tb)
                self.dataset.add(taxa_block)

                #nab = ncl_nxs_reader_handle.GetNumAssumptionsBlocks(ncl_tb)
                #for k in xrange(nab):
                #    a = ncl_nxs_reader_handle.GetAssumptionsBlock(ncl_tb, k)
                #    cs = a.GetTaxSetNames()
                #    print "TaxSets have the names " , str(cs)

                _LOG.debug("Calling MultiFormatReader.GetNumCharactersBlocks()")
                num_char_blocks = ncl_nxs_reader_handle.GetNumCharactersBlocks(ncl_tb)
                for j in xrange(num_char_blocks):
                    _LOG.debug("Calling MultiFormatReader.GetCharactersBlock(taxablock, %d)" % j)
                    ncl_cb = ncl_nxs_reader_handle.GetCharactersBlock(ncl_tb, j)
                    char_block = self._ncl_characters_block_to_native(taxa_block, ncl_cb, ncl_nxs_reader_handle)
                    if char_block:
                        self.dataset.add(char_block)
                _LOG.debug("Calling MultiFormatReader.GetNumTreesBlocks()")
                ntrb = ncl_nxs_reader_handle.GetNumTreesBlocks(ncl_tb)
                for j in xrange(ntrb):
                    trees_block = dataobject.TreeList()
                    trees_block.taxon_set = taxa_block
                    _LOG.debug("Calling MultiFormatReader.GetTreesBlock(%d)" % j)
                    ncl_trb = ncl_nxs_reader_handle.GetTreesBlock(ncl_tb, j)
                    for k in xrange(ncl_trb.GetNumTrees()):
                        ftd = ncl_trb.GetFullTreeDescription(k)
                        tokens = ftd.GetTreeTokens()
                        rooted_flag = ftd.IsRooted()
                        t = self._ncl_tree_tokens_to_native_tree(ncl_tb, taxa_block, tokens, rooted_flag=rooted_flag)
                        if t:
                            trees_block.append(t)
                    self.dataset.add(trees_block)
            return self.dataset


        def tree_source_iter(self, stream, **kwargs):
            """
            Generator to iterate over trees in data file.
            Primary goal is to be memory efficient, storing no more than one tree
            at a time. Speed might have to be sacrificed for this!
            """
            self.update_directives(**kwargs)
            taxa_block = self.get_default_taxon_set()
            if taxa_block is not None and len(taxa_block) == 0:
                self._taxa_to_fill = taxa_block
            else:
                self._taxa_to_fill = None
            n, use_ncl = self._get_fp(file_obj)
            if not use_ncl:
                self.purePythonReader.update_directives(**kwargs)
                self.purePythonReader.encode_splits = self.encode_splits
                self.purePythonReader.rooting_interpreter = self.rooting_interpreter
                self.purePythonReader.finish_node_func = self.finish_node_func
                for tree in self.purePythonReader.tree_source_iter(file_obj, taxon_set=taxa_block, dataset=dataset):
                    yield tree
                return
            if dataset is None:
                dataset = dataobject.DataSet()
            if taxa_block is None:
                taxa_block = dataobject.TaxonSet()
            if taxa_block and not (taxa_block in dataset.taxon_sets):
                dataset.add(taxa_block)

            need_tree_event = Event()
            tree_ready_event = Event()
            die_event = Event()
            ntst = NCLTreeStreamThread(n, need_tree_event=need_tree_event, ready_event=tree_ready_event, die_event=die_event, format=self.format)

            ncl_streamer = ntst.nts

            self._register_taxa_context(ntst.reader, dataset.taxa_blocks)

            ntst.start()
            try:
                need_tree_event.set()
                self.curr_tree_tokens = None
                self.curr_tree = None
                while True:
                    if ntst.done:
                        break
                    tree_ready_event.wait()
                    tree_ready_event.clear()
                    ncl_taxa_block = ncl_streamer.ncl_taxa_block

                    self.curr_tree_tokens = ncl_streamer.tree_tokens
                    if self.curr_tree_tokens is None:
                        break
                    rooted_flag = ncl_streamer.rooted_flag
                    ncl_streamer.tree_tokens = None
                    need_tree_event.set()
                    self.curr_tree = self._ncl_tree_tokens_to_native_tree(ncl_taxa_block, None, self.curr_tree_tokens, rooted_flag=rooted_flag)
                    if self.curr_tree:
                        yield self.curr_tree
                del self.curr_tree_tokens
                del self.curr_tree
            except Exception, v:
                _LOG.debug("%s" % str(v))
                die_event.set()
                need_tree_event.set()
                raise
            if ntst.exception:
                raise ntst.exception
            die_event.set()

        def _register_taxa_context(self, ncl_reader, incoming_taxa_blocks):
            if not incoming_taxa_blocks:
                return

            num_taxa_blocks = ncl_reader.GetNumTaxaBlocks()
            _LOG.debug("Registering previously read taxa blocks.  Currently %d.\nIncoming = %s" % (num_taxa_blocks, str(incoming_taxa_blocks)))
            existing_taxa_blocks = []
            for i in xrange(num_taxa_blocks):
                ncl_tb = ncl_reader.GetTaxaBlock(i)
                labels = list(ncl_tb.GetAllLabels())
                existing_taxa_blocks.append(ncl_tb, labels)

            to_add = []
            for tb in incoming_taxa_blocks:
                if tb or True:
                    found = False
                    l = [i.label for i in tb]
                    for k, v in existing_taxa_blocks:
                        if l == v:
                            found = True
                            self.ncl_taxa_to_native[k.GetInstanceIdentifierString()] = tb
                            break
                    if not found:
                        to_add.append(tb)

            for tb in to_add:
                tn = tuple([i.label for i in tb])
                _LOG.debug("RegisterTaxa(%s)" % str(tn))
                ncl_tb = ncl_reader.RegisterTaxa(tn)
                if ncl_tb:
                    iid = ncl_tb.GetInstanceIdentifierString()
                    self.ncl_taxa_to_native[iid] = tb

        def _ncl_taxa_block_to_native(self, ncl_tb):
            _LOG.debug("Converting NCL taxa block to native")
            _LOG.debug("calling NxsTaxaBlock.GetInstanceIdentifierString()")
            tbiid = ncl_tb.GetInstanceIdentifierString()
            _LOG.debug("got %s" % tbiid)
            taxa_block = self.ncl_taxa_to_native.get(tbiid)
            if taxa_block is not None:
                return taxa_block

            _LOG.debug("calling NxsTaxaBlock.GetAllLabels()")
            labels = ncl_tb.GetAllLabels()
            _LOG.debug("labels = %s" % ' '.join(labels))
            if self._taxa_to_fill is None:
                taxa_block =  dataobject.TaxonSet(labels)
            else:
                taxa_block = self._taxa_to_fill
                self._taxa_to_fill = None
                taxa_block.extend([dataobject.Taxon(label=i) for i in labels])
            self.ncl_taxa_to_native[tbiid] = taxa_block
            return taxa_block

        def _ncl_tree_tokens_to_native_tree(self, ncl_tb, taxa_block, tree_tokens, rooted_flag=None):
            if not tree_tokens:
                return None
            if taxa_block is None:
                iid = ncl_tb.GetInstanceIdentifierString()
                taxa_block = self._ncl_taxa_block_to_native(ncl_tb)
            self.taxa_block = taxa_block
            lti = ListOfTokenIterator(tree_tokens)
            lti.tree_rooted = rooted_flag
            if not self._prev_taxa_block is taxa_block:
                self.tree_translate_dict = {}
                for n, t in enumerate(taxa_block):
                    self.tree_translate_dict[str(n + 1)] = t
                    if self.encode_splits:
                        t.clade_mask = (1 << n)
                self._prev_taxa_block = taxa_block
            return nexustokenizer.parse_tree_from_stream(lti,
                                            taxon_set=taxa_block,
                                            translate_dict=self.tree_translate_dict,
                                            encode_splits=self.encode_splits,
                                            rooting_interpreter=self.rooting_interpreter,
                                            finish_node_func=self.finish_node_func)

        def _ncl_characters_block_to_native(self, taxa_block, ncl_cb, ncl_nxs_reader_handle):
            """
            Processes a FORMAT command. Assumes that the file reader is
            positioned right after the "FORMAT" token in a FORMAT command.
            """
            raw_matrix = ncl_cb.GetRawDiscreteMatrixRef()
            if ncl_cb.IsMixedType():
                _LOG.warn("Mixed datatype character blocks are not supported in Dendropy.  Skipping...")
                return None
            char_block_type = _ncl_datatype_enum_to_dendropy(ncl_cb.GetDataType())
            mapper = ncl_cb.GetDatatypeMapperForCharRef(0)
            symbols = mapper.GetSymbols()
            state_codes_mapping = mapper.GetPythonicStateVectors()

            char_block = char_block_type()
            char_block.taxon_set = taxa_block
            if isinstance(char_block, dataobject.StandardCharacterMatrix):
                sa = dataobject.get_state_alphabet_from_symbols(
                        symbols=symbols,
                        gap_symbol='-',
                        missing_symbol='?'
                )
                char_block.state_alphabets = [sa]
                char_block.default_state_alphabet = char_block.state_alphabets[0]
            symbol_state_map = char_block.default_state_alphabet.symbol_state_map()

            ncl_numeric_code_to_state = []
            for s in symbols:
                ncl_numeric_code_to_state.append(symbol_state_map[s])
            for sc in state_codes_mapping[len(symbols):-2]:
                search = set()
                for fundamental_state in sc:
                    search.add(ncl_numeric_code_to_state[fundamental_state])
                found = False
                for sym, state in symbol_state_map.iteritems():
                    ms = state.member_states
                    if ms:
                        possible = set(ms)
                        if possible == search:
                            found = True
                            ncl_numeric_code_to_state.append(state)
                            break
                if not found:
                    raise ValueError("NCL datatype cannot be coerced into datatype because ambiguity code for %s is missing " % str(search))
            ncl_numeric_code_to_state[-2] = symbol_state_map['-']
            ncl_numeric_code_to_state[-1] = symbol_state_map['?']

            assert (len(raw_matrix) == len(taxa_block))
            for row_ind, taxon in enumerate(taxa_block):
                v = dataobject.CharacterDataVector(taxon=taxon)
                raw_row = raw_matrix[row_ind]
                char_block[taxon] = v
                if not self.exclude_chars:
                    for c in raw_row:
                        state = ncl_numeric_code_to_state[c]
                        v.append(dataobject.CharacterDataCell(value=state))

            #dataset.characters_blocks.append(char_block)
            supporting_exsets = False
            supporting_charset_exsets = False

            if supporting_exsets:
                s = ncl_cb.GetExcludedIndexSet()
                print "Excluded chars =", str(nclwrapper.NxsSetReader.GetSetAsVector(s))
            if supporting_charset_exsets:
                _LOG.debug("Calling MultiFormatReader.GetNumTaxaBlocks()")
                nab = ncl_nxs_reader_handle.GetNumAssumptionsBlocks(ncl_cb)
                for k in xrange(nab):
                    _LOG.debug("Calling MultiFormatReader.GetNumTaxaBlocks()")
                    a = ncl_nxs_reader_handle.GetAssumptionsBlock(ncl_cb, k)
                    cs = a.GetCharSetNames()
                    print "CharSets have the names " , str(cs)
            return char_block

    NexusReader = NCLBasedReader
