import numpy as _np
import sys as _sys
import fastmmap
import pandas as _pd
import tqdm as _tqdm
from . import seq as _seq
import os as _os
import scipy.stats as _ss

_byte_LuT = [None]*256
for i in _np.r_[0:256].astype(_np.uint8):
	_byte_LuT[i] = _np.flatnonzero(_np.unpackbits(i))

# Replace MAF spec column names with human readable oens
def standardize_maf(m):
	rf = [
        ['gene','Hugo_Symbol','Gene_name'],
        ['patient','pat','Tumor_Sample_Barcode','Patient_name','sample'],
        ['chr','Chromosome'],
        ['pos','Position','start','Start_position','Start_Position'],
        ['ref_allele','Reference_Allele','ref'],
        ['newbase','Tumor_Allele','Tum_allele','Alt_allele','Alternate_allele','Tumor_Seq_Allele2','tum_allele2'],
        ['type','Variant_Classification'],
        ['classification','Variant_Type']]

	f = m.columns
	for i in range(0,len(rf)):
		matches = f[f.isin(rf[i])]
		if len(matches)==0:
			continue
		elif len(matches)>1:
			print(f"Mutation file contains multiple columns for {rf[i][0]} info:")
			print(matches)
			
		matches = rf[i][0] if rf[i][0] in matches else matches[0]
		m = m.rename(columns={matches : rf[i][0]})
	return(m)

# Converts single maf dataframe to M format
# Unlike matlab version, string indices are used
def maf2M(m):
	M = dict()
	M['mut'] = m
	
	for f in ['patient','gene']:
		if f in m.columns:
			u,uj,ct = _np.unique(m[f],return_inverse=True,return_counts=True)
			M[f] = _pd.DataFrame(index=u)
			M[f]['nmut'] = ct
			M['mut'][f+'_idx'] = uj

	return(M)


def filter_mutations_against_gnomAD(M, ref = None, field_map = None, gnomad_dir = None):
	# set gnomAD bitwise track reference directory
	_seq.set_gnomad_ref_params(gnomad_dir = gnomad_dir)

	# assume default columns "chr", "pos", "ref", "newbase"
	if field_map is None:
		field_map = dict(zip(M.columns, range(0, len(M.columns))))

	if not _np.all([x in field_map for x in ["chr", "pos", "ref", "newbase"]]):
		raise KeyError("Default fieldname not found in columns!")

	# get column names of chromosome, position, reference/mutant alleles
	chr_f = M.columns[field_map["chr"]]
	pos_f = M.columns[field_map["pos"]]
	ref_f = M.columns[field_map["ref"]]
	newbase_f = M.columns[field_map["newbase"]]

	if not _np.issubdtype(M[chr_f].dtype, _np.integer):
		raise ValueError("Chromosomes must be specified as integers, 1-24")

	M["SSNV_idx"] = M[ref_f].isin(list("ACGT")) & M[newbase_f].isin(list("ACGT"))

	# store overlapping coordinates in dataframe
	O = []
	
	# loop over chromosomes; query gnomAD for each
	for ch, Mc in _tqdm.tqdm(M.groupby(chr_f)):
		# gnomAD has no sex chromosome variants
		if ch >= 23:
			break

		z = _np.zeros(_seq.get_chrlens(ref = ref)[ch - 1], dtype = _np.bool);

		# get 1 bit packed gnomAD representations for:
		# - all alleles
		# - SNP-specific alleles
		for base in ["all"] + list("ACGT"):
			m = _np.copy(z)
			if base != "all":
				_seq.set_gnomad_ref_params(bin_stem = "to_" + base)

				m[Mc.loc[Mc["SSNV_idx"] & (Mc[newbase_f] == base), pos_f] - 1] = True;
			else:
				m[Mc[pos_f] - 1] = True;

			# pack mutation coordinates into 1 bit array
			m = _np.packbits(m)

			# get gnomAD packed array
			g = _seq.query_gnomad_1bit_raw(ch);

			# compute bitwise intersection; index nonzero sites
			bitwise_overlap = g & m 
			bwol_idx = _np.flatnonzero(bitwise_overlap)

			if len(bwol_idx) > 0:
				# unpack bitwise intersection to coordinates and append to DF
				ol_pos = _np.hstack([
						_byte_LuT[byte] + 8*idx for byte, idx in
						zip(bitwise_overlap[bwol_idx], bwol_idx)
				]) + 1
				O.append(_pd.DataFrame({ "chr" : ch, "pos" : ol_pos, "allele" : base }))

	O = _pd.concat(O)

	# intersect with mutations
	M["gpos"] = _seq.chrpos2gpos(M["chr"], M["pos"], ref = ref)
	O["gpos"] = _seq.chrpos2gpos(O["chr"], O["pos"], ref = ref)

	for base in ["all"] + list("ACGT"):
		M["gnomAD_" + base] = M["gpos"].isin(O.loc[O["allele"] == base, "gpos"])

	return M.drop(columns = ["gpos", "SSNV_idx"])

def filter_mutations_against_token_PoN(M, ponfile, ref = None):
	if not all([x in M.columns for x in ["n_ref", "n_alt"]]):
		print("You must provide alt/refcounts as MAF columns n_alt/n_ref, respectively!", file = _sys.stderr)
		return

	tok_hist = get_pon(M, ponfile, ref = ref)

	# get beta distribution densities within each AF bin
	beta_dens = _np.diff(_ss.beta.cdf(
	 _np.r_[0, .001, .003, .03, .2, 1][None, :],
	  M["n_alt"][:, None] + 1,
	  M["n_ref"][:, None] + 1
	), 1)

	# dot this with cumulative distribution (upper) of relevant tokens
	tok_hist_subset = tok_hist[:, 2:7]
	tok_hist_subset[:, -1] += tok_hist[:, -1]
	tok_cum_dist = _np.flip(_np.flip(tok_hist_subset, 1).cumsum(1), 1)/(tok_hist[0, :].sum())

	return _np.log10(_np.sum(beta_dens*tok_cum_dist, 1) + 1e-20)

def get_pon(M, ponfile, ref = None):
	if not _os.path.isfile(ponfile):
		print("Path to PoN file {} not found!".format(ponfile), file = _sys.stderr) 
		return

	gpos = _np.array(_seq.chrpos2gpos(M["chr"], M["pos"], ref = ref))

	return fastmmap.query(
	             ponfile,
	             2,
	             _np.add.outer(8*gpos, _np.r_[0:8]).ravel()
	           ).reshape([-1, 8])

def map_mutations_to_targets(M, T, allow_multimap = False, inplace = True, chrcol = "chr", poscol = "pos", startcol = "start", endcol = "end"):
	Ma = M.loc[:, [chrcol, poscol]].reset_index(drop = True).reset_index().sort_values([chrcol, poscol]).to_numpy()
	Ta = T.loc[:, [chrcol, startcol, endcol]].reset_index(drop = True).reset_index().sort_values([chrcol, startcol, endcol]).to_numpy()

	i = 0
	d = {}
	for m in Ma:
		# advance targets until target start <= mutation position
		while (m[1] > Ta[i, 1] or (m[2] > Ta[i, 3] and m[1] == Ta[i, 1])) and i < Ta.shape[0] - 1:
			i += 1

		# loop over all targets that mutation may overlap
		j = 0
		while i + j < Ta.shape[0] and m[2] >= Ta[i + j, 2] and m[2] <= Ta[i + j, 3] and m[1] == Ta[i + j, 1]:
			if allow_multimap:
				raise NotImplementedError("Mapping to mutiple overlapping targets not yet supported ")
				if m[0] not in d:
					d[m[0]] = {Ta[i + j, 0]}
				else:
					d[m[0]].add(Ta[i + j, 0])
				j += 1
			else:
				d[m[0]] = Ta[i + j, 0]
				break

	d = _pd.Series(d)
	if inplace:
		M["targ_idx"] = -1
		M.loc[d.index, "targ_idx"] = d
	else:
		return d

def convert_chr(chrnames):
	# maps (chr)1-22,X,Y,MT -> 1-22,23,24,0
	chrrange = list(range(0, 25))
	names = ["chr" + str(x) for x in ["MT"] + list(range(1, 23)) + ["X", "Y"]] + \
            [str(x) for x in ["MT"] + list(range(1, 23)) + ["X", "Y"]]
	mapper = dict(zip(names, 2*chrrange))
	return _pd.Series(chrnames).apply(lambda x : mapper[x] if x in mapper else x)

def convert_chr_back(chridxs):
	# maps  1-22,23,24,0 -> chr1-22,X,Y,MT
	chrrange = list(range(0, 25))
	names = ["chr" + str(x) for x in ["MT"] + list(range(1, 23)) + ["X", "Y"]]
	mapper = dict(zip(chrrange, names))
	return _pd.Series(chridxs).apply(lambda x : mapper[x] if x in mapper else x)
