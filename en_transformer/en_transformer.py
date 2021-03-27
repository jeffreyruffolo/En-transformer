import torch
import torch.nn.functional as F
from torch import nn, einsum

from einops import rearrange, repeat
from einops.layers.torch import Rearrange

# helper functions

def exists(val):
    return val is not None

def batched_index_select(values, indices, dim = 1):
    value_dims = values.shape[(dim + 1):]
    values_shape, indices_shape = map(lambda t: list(t.shape), (values, indices))
    indices = indices[(..., *((None,) * len(value_dims)))]
    indices = indices.expand(*((-1,) * len(indices_shape)), *value_dims)
    value_expand_len = len(indices_shape) - (dim + 1)
    values = values[(*((slice(None),) * dim), *((None,) * value_expand_len), ...)]

    value_expand_shape = [-1] * len(values.shape)
    expand_slice = slice(dim, (dim + value_expand_len))
    value_expand_shape[expand_slice] = indices.shape[expand_slice]
    values = values.expand(*value_expand_shape)

    dim += value_expand_len
    return values.gather(dim, indices)

def fourier_encode_dist(x, num_encodings = 4, include_self = True):
    x = x.unsqueeze(-1)
    device, dtype, orig_x = x.device, x.dtype, x
    scales = 2 ** torch.arange(num_encodings, device = device, dtype = dtype)
    x = x / scales
    x = torch.cat([x.sin(), x.cos()], dim=-1)
    x = torch.cat((x, orig_x), dim = -1) if include_self else x
    return x

# classes

# this follows the same strategy for normalization as done in SE3 Transformers
# https://github.com/lucidrains/se3-transformer-pytorch/blob/main/se3_transformer_pytorch/se3_transformer_pytorch.py#L95

class CoorsNorm(nn.Module):
    def __init__(self, eps = 1e-8):
        super().__init__()
        self.eps = eps
        self.fn = nn.Sequential(nn.LayerNorm(1), nn.GELU())

    def forward(self, coors):
        norm = coors.norm(dim = -1, keepdim = True)
        normed_coors = coors / norm.clamp(min = self.eps)
        phase = self.fn(norm)
        return (phase * normed_coors)

class PreNorm(nn.Module):
    def __init__(self, dim, fn):
        super().__init__()
        self.fn = fn
        self.norm = nn.LayerNorm(dim)

    def forward(self, feats, coors, **kwargs):
        feats = self.norm(feats)
        feats, coors = self.fn(feats, coors, **kwargs)
        return feats, coors

class Residual(nn.Module):
    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def forward(self, feats, coors, **kwargs):
        feats_out, coors_delta = self.fn(feats, coors, **kwargs)
        return feats + feats_out, coors + coors_delta

class GEGLU(nn.Module):
    def forward(self, x):
        x, gates = x.chunk(2, dim = -1)
        return x * F.gelu(gates)

class FeedForward(nn.Module):
    def __init__(
        self,
        *,
        dim,
        mult = 4,
        dropout = 0.
    ):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(dim, dim * 4 * 2),
            GEGLU(),
            nn.Dropout(dropout),
            nn.Linear(dim * 4, dim)
        )

    def forward(self, feats, coors):
        return self.net(feats), 0

class EquivariantAttention(nn.Module):
    def __init__(
        self,
        *,
        dim,
        dim_head = 64,
        heads = 4,
        edge_dim = 0,
        m_dim = 16,
        fourier_features = 4,
        norm_rel_coors = False,
        norm_coor_weights = False,
        num_nearest_neighbors = 0,
        only_sparse_neighbors = False,
        valid_neighbor_radius = float('inf'),
        init_eps = 1e-3
    ):
        super().__init__()
        self.fourier_features = fourier_features
        self.num_nearest_neighbors = num_nearest_neighbors
        self.only_sparse_neighbors = only_sparse_neighbors
        self.valid_neighbor_radius = valid_neighbor_radius

        attn_inner_dim = heads * dim_head
        self.heads = heads
        self.to_qkv = nn.Linear(dim, attn_inner_dim * 3, bias = False)
        self.to_out = nn.Linear(attn_inner_dim, dim)

        pos_dim = (fourier_features * 2) + 1
        edge_input_dim = dim_head + edge_dim

        self.to_pos_emb = nn.Sequential(
            nn.Linear(pos_dim, dim_head * 2),
            nn.ReLU(),
            nn.Linear(dim_head * 2, dim_head)
        )

        self.edge_mlp = nn.Sequential(
            nn.Linear(edge_input_dim, edge_input_dim * 2),
            nn.ReLU(),
            nn.Linear(edge_input_dim * 2, m_dim),
            nn.ReLU()
        )

        self.to_attn_mlp = nn.Sequential(
            nn.Linear(m_dim, m_dim * 4),
            nn.ReLU(),
            nn.Linear(m_dim * 4, 1),
            Rearrange('... () -> ...')
        )

        self.coors_mlp = nn.Sequential(
            nn.Linear(m_dim * heads, m_dim * 4),
            nn.ReLU(),
            nn.Linear(m_dim * 4, 1),
            Rearrange('... () -> ...'),
            nn.TanH() if norm_coor_weights else nn.Identity()
        )

        self.rel_coors_norm = CoorsNorm() if norm_rel_coors else nn.Identity()

        self.init_eps = init_eps
        self.apply(self.init_)

    def init_(self, module):
        if type(module) in {nn.Linear}:
            nn.init.normal_(module.weight, std = self.init_eps)

    def forward(
        self,
        feats,
        coors,
        edges = None,
        mask = None,
        adj_mat = None
    ):
        b, n, d, h, fourier_features, num_nn, only_sparse_neighbors, valid_neighbor_radius, device = *feats.shape, self.heads, self.fourier_features, self.num_nearest_neighbors, self.only_sparse_neighbors, self.valid_neighbor_radius, feats.device

        assert not (only_sparse_neighbors and not exists(adj_mat)), 'adjacency matrix must be passed in if only_sparse_neighbors is turned on'

        if exists(mask):
            num_nodes = mask.sum(dim = -1)

        rel_coors = rearrange(coors, 'b i d -> b i () d') - rearrange(coors, 'b j d -> b () j d')
        rel_dist = rel_coors.norm(dim = -1, p = 2)

        nbhd_indices = None
        if num_nn > 0:
            nbhd_ranking = rel_dist

            # make sure padding does not end up becoming neighbors
            if exists(mask):
                ranking_mask = mask[:, :, None] * mask[:, None, :]
                nbhd_ranking = nbhd_ranking.masked_fill(~ranking_mask, 1e5)

            nbhd_indices = nbhd_ranking.topk(num_nn, dim = -1, largest = False).indices

        rel_dist = rearrange(rel_dist, 'b i j -> b i j ()')

        if fourier_features > 0:
            rel_dist = fourier_encode_dist(rel_dist, num_encodings = fourier_features)
            rel_dist = rearrange(rel_dist, 'b i j () d -> b i j d')

        rel_dist = repeat(rel_dist, 'b i j d -> b h i j d', h = h)

        # derive queries keys and values

        q, k, v = self.to_qkv(feats).chunk(3, dim = -1)
        q, k, v = map(lambda t: rearrange(t, 'b n (h d) -> b h n d', h = h), (q, k, v))

        # calculate nearest neighbors

        i = j = n

        if exists(nbhd_indices):
            i, j = nbhd_indices.shape[-2:]
            nbhd_indices_with_heads = repeat(nbhd_indices, 'b n d -> b h n d', h = h)
            k         = batched_index_select(k, nbhd_indices_with_heads, dim = 2)
            v         = batched_index_select(v, nbhd_indices_with_heads, dim = 2)
            rel_dist  = batched_index_select(rel_dist, nbhd_indices_with_heads, dim = 3)
            rel_coors = batched_index_select(rel_coors, nbhd_indices, dim = 2)
        else:
            k = repeat(k, 'b h j d -> b h n j d', n = n)
            v = repeat(v, 'b h j d -> b h n j d', n = n)

        rel_dist_pos_emb = self.to_pos_emb(rel_dist)

        # inject position into values

        v = v + rel_dist_pos_emb

        # prepare mask

        if exists(mask):
            q_mask = rearrange(mask, 'b i -> b () i ()')
            k_mask = repeat(mask, 'b j -> b i j', i = n)

            if exists(nbhd_indices):
                k_mask = batched_index_select(k_mask, nbhd_indices, dim = 2)

            k_mask = rearrange(k_mask, 'b i j -> b () i j')
            mask = q_mask * k_mask

        # expand queries and keys for concatting

        q = repeat(q, 'b h i d -> b h i n d', n = j)

        edge_input = (q - k) + rel_dist_pos_emb

        if exists(edges):
            if exists(nbhd_indices):
                edges = batched_index_select(edges, nbhd_indices, dim = 2)

            edges = repeat(edges, 'b i j d -> b h i j d', h = h)
            edge_input = torch.cat((edge_input, edges), dim = -1)

        m_ij = self.edge_mlp(edge_input)

        coor_mlp_input = rearrange(m_ij, 'b h i j d -> b i j (h d)')
        coor_weights = self.coors_mlp(coor_mlp_input)

        if exists(mask):
            coor_mask = rearrange(mask, 'b () i j -> b i j')
            coor_weights.masked_fill_(~coor_mask, 0.)

        rel_coors = self.rel_coors_norm(rel_coors)
        coors_out = einsum('b i j, b i j c -> b i c', coor_weights, rel_coors)

        # derive attention

        sim = self.to_attn_mlp(m_ij)

        if exists(mask):
            max_neg_value = -torch.finfo(sim.dtype).max
            sim.masked_fill_(~mask, max_neg_value)

        attn = sim.softmax(dim = -1)

        # weighted sum of values and combine heads

        out = einsum('b h i j, b h i j d -> b h i d', attn, v)
        out = rearrange(out, 'b h n d -> b n (h d)')
        out = self.to_out(out)

        return out, coors_out

# transformer

class EnTransformer(nn.Module):
    def __init__(
        self,
        *,
        dim,
        depth,
        num_tokens = None,
        dim_head = 64,
        heads = 8,
        edge_dim = 0,
        m_dim = 16,
        fourier_features = 4,
        num_nearest_neighbors = 0,
        only_sparse_neighbors = False,
        valid_neighbor_radius = float('inf'),
        norm_rel_coors = False,
        norm_coor_weights = False,
        init_eps = 1e-3
    ):
        super().__init__()
        self.token_emb = nn.Embedding(num_tokens, dim) if exists(num_tokens) else None

        self.layers = nn.ModuleList([])
        for _ in range(depth):
            self.layers.append(nn.ModuleList([
                Residual(PreNorm(dim, EquivariantAttention(dim = dim, dim_head = dim_head, heads = heads, m_dim = m_dim, edge_dim = edge_dim, fourier_features = fourier_features, norm_rel_coors = norm_rel_coors,  norm_coor_weights = norm_coor_weights, num_nearest_neighbors = num_nearest_neighbors, only_sparse_neighbors = only_sparse_neighbors, valid_neighbor_radius = valid_neighbor_radius, init_eps = init_eps))),
                Residual(PreNorm(dim, FeedForward(dim = dim)))
            ]))

        self.num_nearest_neighbors = num_nearest_neighbors

    def forward(
        self,
        feats,
        coors,
        edges = None,
        mask = None,
        adj_mat = None
    ):
        if exists(self.token_emb):
            feats = self.token_emb(feats)

        # main network

        for attn, ff in self.layers:
            feats, coors = attn(feats, coors, edges = edges, mask = mask, adj_mat = adj_mat)
            feats, coors = ff(feats, coors)

        return feats, coors
