
import os
import torch
import pandas as pd
import pyterrier as pt
from pyterrier import tqdm
from pyterrier.transformer import TransformerBase

import random
from colbert.evaluation.load_model import load_model
from colbert.modeling.inference import ModelInference
from colbert.evaluation.slow import slow_rerank
from colbert.indexing.loaders import get_parts, load_doclens
from colbert.indexing.faiss import get_faiss_index_name
from colbert.ranking.faiss_index import FaissIndex
from colbert.ranking.faiss_term_index import FaissNNTerm
from collections import defaultdict
import numpy as np
from warnings import warn

class file_part_mmap:
    def __init__(self, file_path, file_doclens):
        self.dim = 128 # TODO
        
        self.doclens = file_doclens
        self.endpos = np.cumsum(self.doclens)
        self.startpos = self.endpos - self.doclens

        mmap_storage = torch.HalfStorage.from_file(file_path, False, sum(self.doclens) * self.dim)
        self.mmap = torch.HalfTensor(mmap_storage).view(sum(self.doclens), self.dim)
 
    def get_embedding(self, pid):
        startpos = self.startpos[pid]
        endpos = self.endpos[pid]
        return self.mmap[startpos:endpos,:]

class file_part_mem:
    def __init__(self, file_path, file_doclens):
        self.dim = 128 # TODO
        
        self.doclens = file_doclens
        self.endpos = np.cumsum(self.doclens)
        self.startpos = self.endpos - self.doclens

        self.mmap = torch.load(file_path)
        #print(self.mmap.shape)
 
    def get_embedding(self, pid):
        startpos = self.startpos[pid]
        endpos = self.endpos[pid]
        return self.mmap[startpos:endpos,:]


class Object(object):
    pass


from typing import List     


class re_ranker_mmap:
    def __init__(self, index_path, args, inference, verbose = False, memtype='mmap'):
        self.args = args
        self.doc_maxlen = args.doc_maxlen
        assert self.doc_maxlen > 0
        self.inference = inference
        self.dim = 128 #TODO
        self.verbose = verbose
    
        # Every pt file gets its own list of doc lengths
        self.part_doclens = load_doclens(index_path, flatten=False)
        assert len(self.part_doclens) > 0, "Did not find any indices at %s" % index_path
        # Local mmapped tensors with local, single file accesses
        self.part_mmap : List[file_part_mmap] = re_ranker_mmap._load_parts(index_path, self.part_doclens, memtype)
        
        # last pid (inclusive, e.g., the -1) in each pt file
        # the -1 is used in the np.searchsorted
        # so if each partition has 1000 docs, the array is [999, 1999, ...]
        # this helps us map from passage id to part (inclusive, explaning the -1)
        self.part_pid_end_offsets = np.cumsum([len(x) for x in self.part_doclens]) - 1
        
        # first pid (inclusive) in each pt file
        tmp = np.cumsum([len(x) for x in self.part_doclens])
        tmp[-1] = 0
        self.part_pid_begin_offsets = np.roll(tmp, 1)
        # [0, 1000, 2000, ...]
        self.part_pid_begin_offsets
    
    @staticmethod
    def _load_parts(index_path, part_doclens, memtype="mmap"):
        # Every pt file is loaded and managed independently, with local pids
        _, all_parts_paths, _ = get_parts(index_path)
        
        if memtype == "mmap":
            all_parts_paths = [ file.replace(".pt", ".store") for file in all_parts_paths ]
            mmaps = [file_part_mmap(path, doclens) for path, doclens in zip(all_parts_paths, part_doclens)]
        elif memtype == "mem":
            mmaps = [file_part_mem(path, doclens) for path, doclens in tqdm(zip(all_parts_paths, part_doclens), total=len(all_parts_paths), desc="Loading index shards to memory", unit="shard")]
        else:
            assert False, "Unknown memtype %s" % memtype
        return mmaps

    def get_embedding(self, pid):
        # In which pt file we need to look the given pid
        part_id = np.searchsorted(self.part_pid_end_offsets, pid)
        # calculate the pid local to the correct pt file
        local_pid = pid - self.part_pid_begin_offsets[part_id]
        # identify the tensor we look for
        disk_tensor = self.part_mmap[part_id].get_embedding(local_pid)
        doclen = disk_tensor.shape[0]
         # only here is there a memory copy from the memory mapped file 
        target = torch.zeros(self.doc_maxlen, self.dim)
        target[:doclen, :] = disk_tensor
        return target
    
    def get_embedding_copy(self, pid, target, index):
        # In which pt file we need to look the given pid
        part_id = np.searchsorted(self.part_pid_end_offsets, pid)
        # calculate the pid local to the correct pt file
        local_pid = pid - self.part_pid_begin_offsets[part_id]
        # identify the tensor we look for
        disk_tensor = self.part_mmap[part_id].get_embedding(local_pid)
        doclen = disk_tensor.shape[0]
         # only here is there a memory copy from the memory mapped file 
        target[index, :doclen, :] = disk_tensor
        return target
    
    def our_rerank(self, query, pids, gpu=True):
        colbert = self.args.colbert
        inference = self.inference

        Q = inference.queryFromText([query])
        if self.verbose:
            pid_iter = tqdm(pids, desc="lookups", unit="d")
        else:
            pid_iter = pids

        D_ = torch.zeros(len(pids), self.doc_maxlen, self.dim)
        for offset, pid in enumerate(pid_iter):
            self.get_embedding_copy(pid, D_, offset)

        if gpu:
            D_ = D_.cuda()

        scores = colbert.score(Q, D_).cpu()
        return scores.tolist()
        
        
    def our_rerank_with_embeddings(self, qembs, pids, weightsQ=None, gpu=True):
        """
        input: qid,query, docid, query_tokens, query_embeddings, query_weights 
        
        output: qid, query, docid, score
        """
        colbert = self.args.colbert
        inference = self.inference
        # default is uniform weight for all query embeddings
        if weightsQ is None:
            weightsQ = torch.ones(len(qembs))
        # make to 3d tensor
        Q = torch.unsqueeze(qembs, 0)
        if gpu:
            Q = Q.cuda()
        #weightE = weightE(Q,E) # calculate the mean_cos score of each expansion term with all query term, the softmax normalised as the weight of the expansion term
        
        if self.verbose:
            pid_iter = tqdm(pids, desc="lookups", unit="d")
        else:
            pid_iter = pids

        D_ = torch.zeros(len(pids), self.doc_maxlen, self.dim)
        for offset, pid in enumerate(pid_iter):
            self.get_embedding_copy(pid, D_, offset)
        if gpu:
            D_ = D_.cuda()
        maxscoreQ = (Q @ D_.permute(0, 2, 1)).max(2).values.cpu()
        scores = (weightsQ*maxscoreQ).sum(1).cpu()
        print(scores)
        return scores.tolist()

class ColBERTFactory():

    def __init__(self, 
            colbert_model : str, 
            index_root : str, 
            index_name : str,
            faiss=True,
            memtype = "mem",
            gpu=True):
        
        args = Object()
        args.query_maxlen = 32
        args.doc_maxlen = 180
        args.dim = 128
        args.bsize = 128
        args.similarity = 'cosine'        
        args.dim = 128
        args.amp = True
        args.nprobe = 10
        args.part_range = None
        args.mask_punctuation = False
        args.partitions = None

        if index_root is None or index_name is None:
            warn("No index_root and index_name specified - no index ranking possible")
        else:
            self.index_path = os.path.join(index_root, index_name)

        if not gpu:
            warn("Gpu disabled, YMMV")
            import colbert.parameters
            colbert.parameters.DEVICE = torch.device("cpu")
        args.checkpoint = colbert_model
        args.colbert, args.checkpoint = load_model(args)
        args.inference = ModelInference(args.colbert, amp=args.amp)
        
        self.args = args
        self.memtype = memtype

        #we load this lazily
        self.rrm = None
        self.faiss_index = None
        
    def _rrm(self):
        """
        Returns an instance of the re_ranker_mmap class.
        Only one is created, if necessary.
        """

        if self.rrm is not None:
            return self.rrm
        print("Loading reranking index, memtype=%s" % self.memtype)
        self.rrm = re_ranker_mmap(
            self.index_path, 
            self.args, 
            self.args.inference, 
            verbose=True, 
            memtype=self.memtype)
        return self.rrm
        
    def nn_term(self, df=False):
        """
        Returns an instance of the FaissNNTerm class, which provides statistics about terms
        """
        return FaissNNTerm(
            self.args.colbert,
            self.index_root,
            self.index_name,
            self._faiss_index(),
            df=df)

    def query_encoder(self, detach=True) -> TransformerBase:
        """
        Returns a transformer that can encode queries using ColBERT's model.
        input: qid, query
        output: qid, query, query_embs, query_toks,
        """
        def _encode_query(row):
            qid = row.qid
            Q, ids, masks = self.args.inference.queryFromText([row.query], bsize=512, with_ids=True)
            if detach:
                Q = Q.cpu()
            return pd.Series([Q[0], ids[0]])
            
        def row_apply(df):
            df[["query_embs", "query_toks"]] = df.apply(_encode_query, axis=1)
            return df
        
        return pt.apply.generic(row_apply)

    def _faiss_index(self):
        """
        Returns an instance of the Colbert FaissIndex class, which provides nearest neighbour information
        """
        if self.faiss_index is not None:
            return self.faiss_index
        faiss_index_path = get_faiss_index_name(self.args)
        faiss_index_path = os.path.join(self.index_path, faiss_index_path)
        self.faiss_index = FaissIndex(self.index_path, faiss_index_path, self.args.nprobe, self.args.part_range)
        return self.faiss_index

    def set_retrieve(self, batch=False, query_encoded=False, faiss_depth=1000, verbose=True) -> TransformerBase:
        #input: qid, query
        #OR
        #input: qid, query, query_embs, query_toks, query_weights

        #output: qid, query, docno
        #OR
        #output: qid, query, query_embs, query_toks, query_weights, docno
        
        assert not batch
        faiss_index = self._faiss_index()
        
        def _single_retrieve(queries_df):
            rtr = []
            iter = queries_df.itertuples()
            iter = tqdm(iter, unit="q") if verbose else iter
            for row in iter:
                qid = row.qid
                Q, ids, masks = self.args.inference.queryFromText([row.query], bsize=512, with_ids=True)
                Q_f = Q[0:1, :, :]
                all_pids = faiss_index.retrieve(faiss_depth, Q_f, verbose=True)
                for passage_ids in all_pids:
                    print("qid %s retrieved docs %d" % (qid, len(passage_ids)))
                    for pid in passage_ids:
                        rtr.append([qid, row.query, str(pid), ids[0], Q[0, :, :].cpu()])
            return pd.DataFrame(rtr, columns=["qid","query",'docno','query_toks','query_embs'])

        def _single_retrieve_qembs(queries_df):
            rtr = []
            iter = queries_df.itertuples()
            iter = tqdm(iter, unit="q") if verbose else iter
            for row in iter:
                qid = row.qid
                embs = row.query_embs
                Q_f = torch.unsqueeze(embs, 0)
                all_pids = faiss_index.retrieve(faiss_depth, Q_f, verbose=True)
                for passage_ids in all_pids:
                    print("qid %s retrieved docs %d" % (qid, len(passage_ids)))
                    for pid in passage_ids:
                        rtr.append([qid, row.query, str(pid), row.query_toks, row.query_embs])
            return pd.DataFrame(rtr, columns=["qid","query",'docno','query_toks','query_embs']) 
        
        return pt.apply.generic(_single_retrieve_qembs if query_encoded else _single_retrieve)

    def text_scorer(self, query_encoded=False, doc_attr="text", verbose=False) -> TransformerBase:
        """
        Returns a transformer that uses ColBERT model to score the *text* of documents.
        """
        #input: qid, query, docno, text
        #OR
        #input: qid, query, query_embs, query_toks, query_weights, docno, text

        #output: qid, query, docno, score

        assert not query_encoded
        def _text_scorer(queries_and_docs):
            groupby = queries_and_docs.groupby("qid")
            rtr=[]
            with torch.no_grad():
                for qid, group in tqdm(groupby, total=len(groupby), unit="q") if verbose else groupby:
                    query = group["query"].values[0]
                    ranking = slow_rerank(self.args, query, group["docno"].values, group[doc_attr].values.tolist())
                    for rank, (score, pid, passage) in enumerate(ranking):
                            rtr.append([qid, query, pid, score, rank])          
            return pd.DataFrame(rtr, columns=["qid", "query", "docno", "score", "rank"])

        return pt.apply.generic(_text_scorer)

    def index_scorer(self, query_encoded=False, add_ranks=False) -> TransformerBase:
        """
        Returns a transformer that uses the ColBERT index to perform scoring of documents to queries 
        """
        #input: qid, query, docno, 
        #OR
        #input: qid, query, query_embs, query_toks, query_weights, docno

        #output: qid, query, docno, score

        rrm = self._rrm()

        def rrm_scorer(qid_group):
            qid_group = qid_group.copy()
            qid_group["docid"] = qid_group["docno"].astype('int64')
            qid_group.sort_values("docid", inplace=True)
            docids = qid_group["docid"].values
            scores = rrm.our_rerank(qid_group.iloc[0]["query"], docids)
            qid_group["score"] = scores
            if add_ranks:
                return pt.model.add_ranks(qid_group)
            return qid_group

        def rrm_scorer_query_embs(qid_group):
            qid_group = qid_group.copy()
            qid_group["docid"] = qid_group["docno"].astype('int')
            qid_group.sort_values("docid", inplace=True)
            docids = qid_group["docid"].values
            weights = None
            if "query_weights" in qid_group.columns:
                weights = qid_group.iloc[0].query_weights
            scores = rrm.our_rerank_with_embeddings(qid_group.iloc[0]["query_embs"], docids, weights)
            qid_group["score"] = scores
            if add_ranks:
                return pt.model.add_ranks(qid_group)
            return qid_group

        if query_encoded:
            return pt.apply.by_query(rrm_scorer_query_embs) 
        return pt.apply.by_query(rrm_scorer) 

