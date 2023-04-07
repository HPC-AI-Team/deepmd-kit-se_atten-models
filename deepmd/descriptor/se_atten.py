from typing import (
    List,
    Optional,
    Tuple,
)

import numpy as np
from packaging.version import (
    Version,
)

from deepmd.common import (
    cast_precision,
)
from deepmd.env import (
    GLOBAL_NP_FLOAT_PRECISION,
    GLOBAL_TF_FLOAT_PRECISION,
    TF_VERSION,
    default_tf_session_config,
    op_module,
    tf,
)
from deepmd.utils.graph import (
    get_attention_layer_variables_from_graph_def,
)
from deepmd.utils.network import (
    embedding_net,
    one_layer,
)
from deepmd.utils.sess import (
    run_sess,
)

from .descriptor import (
    Descriptor,
)
from .se_a import (
    DescrptSeA,
)


@Descriptor.register("se_atten")
class DescrptSeAtten(DescrptSeA):
    r"""Smooth version descriptor with attention.

    Parameters
    ----------
    rcut
            The cut-off radius :math:`r_c`
    rcut_smth
            From where the environment matrix should be smoothed :math:`r_s`
    sel : list[str]
            sel[i] specifies the maxmum number of type i atoms in the cut-off radius
    neuron : list[int]
            Number of neurons in each hidden layers of the embedding net :math:`\mathcal{N}`
    axis_neuron
            Number of the axis neuron :math:`M_2` (number of columns of the sub-matrix of the embedding matrix)
    resnet_dt
            Time-step `dt` in the resnet construction:
            y = x + dt * \phi (Wx + b)
    trainable
            If the weights of embedding net are trainable.
    seed
            Random seed for initializing the network parameters.
    type_one_side
            Try to build N_types embedding nets. Otherwise, building N_types^2 embedding nets
    exclude_types : List[List[int]]
            The excluded pairs of types which have no interaction with each other.
            For example, `[[0, 1]]` means no interaction between type 0 and type 1.
    activation_function
            The activation function in the embedding net. Supported options are |ACTIVATION_FN|
    precision
            The precision of the embedding net parameters. Supported options are |PRECISION|
    uniform_seed
            Only for the purpose of backward compatibility, retrieves the old behavior of using the random seed
    attn
            The length of hidden vector during scale-dot attention computation.
    attn_layer
            The number of layers in attention mechanism.
    attn_dotr
            Whether to dot the relative coordinates on the attention weights as a gated scheme.
    attn_mask
            Whether to mask the diagonal in the attention weights.
    multi_task
            If the model has multi fitting nets to train.
    """

    def __init__(
        self,
        rcut: float,
        rcut_smth: float,
        sel: int,
        ntypes: int,
        neuron: List[int] = [24, 48, 96],
        axis_neuron: int = 8,
        resnet_dt: bool = False,
        trainable: bool = True,
        seed: Optional[int] = None,
        type_one_side: bool = True,
        exclude_types: List[List[int]] = [],
        activation_function: str = "tanh",
        precision: str = "default",
        uniform_seed: bool = False,
        attn: int = 128,
        attn_layer: int = 2,
        attn_dotr: bool = True,
        attn_mask: bool = False,
        multi_task: bool = False,
    ) -> None:
        DescrptSeA.__init__(
            self,
            rcut,
            rcut_smth,
            [sel],
            neuron=neuron,
            axis_neuron=axis_neuron,
            resnet_dt=resnet_dt,
            trainable=trainable,
            seed=seed,
            type_one_side=type_one_side,
            exclude_types=exclude_types,
            set_davg_zero=True,
            activation_function=activation_function,
            precision=precision,
            uniform_seed=uniform_seed,
            multi_task=multi_task,
        )
        """
        Constructor
        """
        assert Version(TF_VERSION) > Version(
            "2"
        ), "se_atten only support tensorflow version 2.0 or higher."
        self.ntypes = ntypes
        self.att_n = attn
        self.attn_layer = attn_layer
        self.attn_mask = attn_mask
        self.attn_dotr = attn_dotr

        # descrpt config
        self.sel_all_a = [sel]
        self.sel_all_r = [0]
        avg_zero = np.zeros([self.ntypes, self.ndescrpt]).astype(
            GLOBAL_NP_FLOAT_PRECISION
        )
        std_ones = np.ones([self.ntypes, self.ndescrpt]).astype(
            GLOBAL_NP_FLOAT_PRECISION
        )
        self.beta = np.zeros([self.attn_layer, self.filter_neuron[-1]]).astype(
            GLOBAL_NP_FLOAT_PRECISION
        )
        self.gamma = np.ones([self.attn_layer, self.filter_neuron[-1]]).astype(
            GLOBAL_NP_FLOAT_PRECISION
        )
        self.attention_layer_variables = None
        sub_graph = tf.Graph()
        with sub_graph.as_default():
            name_pfx = "d_sea_"
            for ii in ["coord", "box"]:
                self.place_holders[ii] = tf.placeholder(
                    GLOBAL_NP_FLOAT_PRECISION, [None, None], name=name_pfx + "t_" + ii
                )
            self.place_holders["type"] = tf.placeholder(
                tf.int32, [None, None], name=name_pfx + "t_type"
            )
            self.place_holders["natoms_vec"] = tf.placeholder(
                tf.int32, [self.ntypes + 2], name=name_pfx + "t_natoms"
            )
            self.place_holders["default_mesh"] = tf.placeholder(
                tf.int32, [None], name=name_pfx + "t_mesh"
            )
            (
                self.stat_descrpt,
                self.descrpt_deriv_t,
                self.rij_t,
                self.nlist_t,
                self.nei_type_vec_t,
                self.nmask_t,
            ) = op_module.prod_env_mat_a_mix(
                self.place_holders["coord"],
                self.place_holders["type"],
                self.place_holders["natoms_vec"],
                self.place_holders["box"],
                self.place_holders["default_mesh"],
                tf.constant(avg_zero),
                tf.constant(std_ones),
                rcut_a=self.rcut_a,
                rcut_r=self.rcut_r,
                rcut_r_smth=self.rcut_r_smth,
                sel_a=self.sel_all_a,
                sel_r=self.sel_all_r,
            )
        self.sub_sess = tf.Session(graph=sub_graph, config=default_tf_session_config)

    def compute_input_stats(
        self,
        data_coord: list,
        data_box: list,
        data_atype: list,
        natoms_vec: list,
        mesh: list,
        input_dict: dict,
        mixed_type: bool = False,
        real_natoms_vec: Optional[list] = None,
    ) -> None:
        """Compute the statisitcs (avg and std) of the training data. The input will be normalized by the statistics.

        Parameters
        ----------
        data_coord
            The coordinates. Can be generated by deepmd.model.make_stat_input
        data_box
            The box. Can be generated by deepmd.model.make_stat_input
        data_atype
            The atom types. Can be generated by deepmd.model.make_stat_input
        natoms_vec
            The vector for the number of atoms of the system and different types of atoms.
            If mixed_type is True, this para is blank. See real_natoms_vec.
        mesh
            The mesh for neighbor searching. Can be generated by deepmd.model.make_stat_input
        input_dict
            Dictionary for additional input
        mixed_type
            Whether to perform the mixed_type mode.
            If True, the input data has the mixed_type format (see doc/model/train_se_atten.md),
            in which frames in a system may have different natoms_vec(s), with the same nloc.
        real_natoms_vec
            If mixed_type is True, it takes in the real natoms_vec for each frame.
        """
        if True:
            sumr = []
            suma = []
            sumn = []
            sumr2 = []
            suma2 = []
            if mixed_type:
                sys_num = 0
                for cc, bb, tt, nn, mm, r_n in zip(
                    data_coord, data_box, data_atype, natoms_vec, mesh, real_natoms_vec
                ):
                    sysr, sysr2, sysa, sysa2, sysn = self._compute_dstats_sys_smth(
                        cc, bb, tt, nn, mm, mixed_type, r_n
                    )
                    sys_num += 1
                    sumr.append(sysr)
                    suma.append(sysa)
                    sumn.append(sysn)
                    sumr2.append(sysr2)
                    suma2.append(sysa2)
            else:
                for cc, bb, tt, nn, mm in zip(
                    data_coord, data_box, data_atype, natoms_vec, mesh
                ):
                    sysr, sysr2, sysa, sysa2, sysn = self._compute_dstats_sys_smth(
                        cc, bb, tt, nn, mm
                    )
                    sumr.append(sysr)
                    suma.append(sysa)
                    sumn.append(sysn)
                    sumr2.append(sysr2)
                    suma2.append(sysa2)
            if not self.multi_task:
                stat_dict = {
                    "sumr": sumr,
                    "suma": suma,
                    "sumn": sumn,
                    "sumr2": sumr2,
                    "suma2": suma2,
                }
                self.merge_input_stats(stat_dict)
            else:
                self.stat_dict["sumr"] += sumr
                self.stat_dict["suma"] += suma
                self.stat_dict["sumn"] += sumn
                self.stat_dict["sumr2"] += sumr2
                self.stat_dict["suma2"] += suma2

    def build(
        self,
        coord_: tf.Tensor,
        atype_: tf.Tensor,
        natoms: tf.Tensor,
        box_: tf.Tensor,
        mesh: tf.Tensor,
        input_dict: dict,
        reuse: Optional[bool] = None,
        suffix: str = "",
    ) -> tf.Tensor:
        """Build the computational graph for the descriptor.

        Parameters
        ----------
        coord_
            The coordinate of atoms
        atype_
            The type of atoms
        natoms
            The number of atoms. This tensor has the length of Ntypes + 2
            natoms[0]: number of local atoms
            natoms[1]: total number of atoms held by this processor
            natoms[i]: 2 <= i < Ntypes+2, number of type i atoms
        box_ : tf.Tensor
            The box of the system
        mesh
            For historical reasons, only the length of the Tensor matters.
            if size of mesh == 6, pbc is assumed.
            if size of mesh == 0, no-pbc is assumed.
        input_dict
            Dictionary for additional inputs
        reuse
            The weights in the networks should be reused when get the variable.
        suffix
            Name suffix to identify this descriptor

        Returns
        -------
        descriptor
            The output descriptor
        """
        davg = self.davg
        dstd = self.dstd
        with tf.variable_scope("descrpt_attr" + suffix, reuse=reuse):
            if davg is None:
                davg = np.zeros([self.ntypes, self.ndescrpt])
            if dstd is None:
                dstd = np.ones([self.ntypes, self.ndescrpt])
            t_rcut = tf.constant(
                np.max([self.rcut_r, self.rcut_a]),
                name="rcut",
                dtype=GLOBAL_TF_FLOAT_PRECISION,
            )
            t_ntypes = tf.constant(self.ntypes, name="ntypes", dtype=tf.int32)
            t_ndescrpt = tf.constant(self.ndescrpt, name="ndescrpt", dtype=tf.int32)
            t_sel = tf.constant(self.sel_a, name="sel", dtype=tf.int32)
            t_original_sel = tf.constant(
                self.original_sel if self.original_sel is not None else self.sel_a,
                name="original_sel",
                dtype=tf.int32,
            )
            self.t_avg = tf.get_variable(
                "t_avg",
                davg.shape,
                dtype=GLOBAL_TF_FLOAT_PRECISION,
                trainable=False,
                initializer=tf.constant_initializer(davg),
            )
            self.t_std = tf.get_variable(
                "t_std",
                dstd.shape,
                dtype=GLOBAL_TF_FLOAT_PRECISION,
                trainable=False,
                initializer=tf.constant_initializer(dstd),
            )

        with tf.control_dependencies([t_sel, t_original_sel]):
            coord = tf.reshape(coord_, [-1, natoms[1] * 3])
        box = tf.reshape(box_, [-1, 9])
        atype = tf.reshape(atype_, [-1, natoms[1]])
        self.attn_weight = [None for i in range(self.attn_layer)]
        self.angular_weight = [None for i in range(self.attn_layer)]
        self.attn_weight_final = [None for i in range(self.attn_layer)]

        (
            self.descrpt,
            self.descrpt_deriv,
            self.rij,
            self.nlist,
            self.nei_type_vec,
            self.nmask,
        ) = op_module.prod_env_mat_a_mix(
            coord,
            atype,
            natoms,
            box,
            mesh,
            self.t_avg,
            self.t_std,
            rcut_a=self.rcut_a,
            rcut_r=self.rcut_r,
            rcut_r_smth=self.rcut_r_smth,
            sel_a=self.sel_all_a,
            sel_r=self.sel_all_r,
        )
        self.nei_type_vec = tf.reshape(self.nei_type_vec, [-1])
        self.nmask = tf.cast(
            tf.reshape(self.nmask, [-1, 1, self.sel_all_a[0]]),
            self.filter_precision,
        )
        self.negative_mask = -(2 << 32) * (1.0 - self.nmask)
        # only used when tensorboard was set as true
        tf.summary.histogram("descrpt", self.descrpt)
        tf.summary.histogram("rij", self.rij)
        tf.summary.histogram("nlist", self.nlist)

        self.descrpt_reshape = tf.reshape(self.descrpt, [-1, self.ndescrpt])
        # prevent lookup error; the actual atype already used for nlist
        atype = tf.clip_by_value(atype, 0, self.ntypes - 1)
        self.atype_nloc = tf.reshape(
            tf.slice(atype, [0, 0], [-1, natoms[0]]), [-1]
        )  ## lammps will have error without this
        self._identity_tensors(suffix=suffix)

        self.dout, self.qmat = self._pass_filter(
            self.descrpt_reshape,
            self.atype_nloc,
            natoms,
            input_dict,
            suffix=suffix,
            reuse=reuse,
            trainable=self.trainable,
        )

        # only used when tensorboard was set as true
        tf.summary.histogram("embedding_net_output", self.dout)
        return self.dout

    def _pass_filter(
        self, inputs, atype, natoms, input_dict, reuse=None, suffix="", trainable=True
    ):
        assert (
            input_dict is not None
            and input_dict.get("type_embedding", None) is not None
        ), "se_atten desctiptor must use type_embedding"
        type_embedding = input_dict.get("type_embedding", None)
        inputs = tf.reshape(inputs, [-1, natoms[0], self.ndescrpt])
        output = []
        output_qmat = []
        inputs_i = inputs
        inputs_i = tf.reshape(inputs_i, [-1, self.ndescrpt])
        type_i = -1
        if len(self.exclude_types):
            mask = self.build_type_exclude_mask(
                self.exclude_types,
                self.ntypes,
                self.sel_a,
                self.ndescrpt,
                atype,
                tf.shape(inputs_i)[0],
                self.nei_type_vec,  # extra input for atten
            )
            inputs_i *= mask

        layer, qmat = self._filter(
            inputs_i,
            type_i,
            natoms,
            name="filter_type_all" + suffix,
            suffix=suffix,
            reuse=reuse,
            trainable=trainable,
            activation_fn=self.filter_activation_fn,
            type_embedding=type_embedding,
            atype=atype,
        )
        layer = tf.reshape(layer, [tf.shape(inputs)[0], natoms[0], self.get_dim_out()])
        qmat = tf.reshape(
            qmat, [tf.shape(inputs)[0], natoms[0], self.get_dim_rot_mat_1() * 3]
        )
        output.append(layer)
        output_qmat.append(qmat)
        output = tf.concat(output, axis=1)
        output_qmat = tf.concat(output_qmat, axis=1)
        return output, output_qmat

    def _compute_dstats_sys_smth(
        self,
        data_coord,
        data_box,
        data_atype,
        natoms_vec,
        mesh,
        mixed_type=False,
        real_natoms_vec=None,
    ):
        dd_all, descrpt_deriv_t, rij_t, nlist_t, nei_type_vec_t, nmask_t = run_sess(
            self.sub_sess,
            [
                self.stat_descrpt,
                self.descrpt_deriv_t,
                self.rij_t,
                self.nlist_t,
                self.nei_type_vec_t,
                self.nmask_t,
            ],
            feed_dict={
                self.place_holders["coord"]: data_coord,
                self.place_holders["type"]: data_atype,
                self.place_holders["natoms_vec"]: natoms_vec,
                self.place_holders["box"]: data_box,
                self.place_holders["default_mesh"]: mesh,
            },
        )
        if mixed_type:
            nframes = dd_all.shape[0]
            sysr = [0.0 for i in range(self.ntypes)]
            sysa = [0.0 for i in range(self.ntypes)]
            sysn = [0 for i in range(self.ntypes)]
            sysr2 = [0.0 for i in range(self.ntypes)]
            sysa2 = [0.0 for i in range(self.ntypes)]
            for ff in range(nframes):
                natoms = real_natoms_vec[ff]
                dd_ff = np.reshape(dd_all[ff], [-1, self.ndescrpt * natoms[0]])
                start_index = 0
                for type_i in range(self.ntypes):
                    end_index = (
                        start_index + self.ndescrpt * natoms[2 + type_i]
                    )  # center atom split
                    dd = dd_ff[:, start_index:end_index]
                    dd = np.reshape(
                        dd, [-1, self.ndescrpt]
                    )  # nframes * typen_atoms , nnei * 4
                    start_index = end_index
                    # compute
                    dd = np.reshape(dd, [-1, 4])  # nframes * typen_atoms * nnei, 4
                    ddr = dd[:, :1]
                    dda = dd[:, 1:]
                    sumr = np.sum(ddr)
                    suma = np.sum(dda) / 3.0
                    sumn = dd.shape[0]
                    sumr2 = np.sum(np.multiply(ddr, ddr))
                    suma2 = np.sum(np.multiply(dda, dda)) / 3.0
                    sysr[type_i] += sumr
                    sysa[type_i] += suma
                    sysn[type_i] += sumn
                    sysr2[type_i] += sumr2
                    sysa2[type_i] += suma2
        else:
            natoms = natoms_vec
            dd_all = np.reshape(dd_all, [-1, self.ndescrpt * natoms[0]])
            start_index = 0
            sysr = []
            sysa = []
            sysn = []
            sysr2 = []
            sysa2 = []
            for type_i in range(self.ntypes):
                end_index = (
                    start_index + self.ndescrpt * natoms[2 + type_i]
                )  # center atom split
                dd = dd_all[:, start_index:end_index]
                dd = np.reshape(
                    dd, [-1, self.ndescrpt]
                )  # nframes * typen_atoms , nnei * 4
                start_index = end_index
                # compute
                dd = np.reshape(dd, [-1, 4])  # nframes * typen_atoms * nnei, 4
                ddr = dd[:, :1]
                dda = dd[:, 1:]
                sumr = np.sum(ddr)
                suma = np.sum(dda) / 3.0
                sumn = dd.shape[0]
                sumr2 = np.sum(np.multiply(ddr, ddr))
                suma2 = np.sum(np.multiply(dda, dda)) / 3.0
                sysr.append(sumr)
                sysa.append(suma)
                sysn.append(sumn)
                sysr2.append(sumr2)
                sysa2.append(suma2)
        return sysr, sysr2, sysa, sysa2, sysn

    def _lookup_type_embedding(
        self,
        xyz_scatter,
        natype,
        type_embedding,
    ):
        """Concatenate `type_embedding` of neighbors and `xyz_scatter`.
        If not self.type_one_side, concatenate `type_embedding` of center atoms as well.

        Parameters
        ----------
        xyz_scatter:
            shape is [nframes*natoms[0]*self.nnei, 1]
        natype:
            neighbor atom type
        type_embedding:
            shape is [self.ntypes, Y] where Y=jdata['type_embedding']['neuron'][-1]

        Returns
        -------
        embedding:
            environment of each atom represented by embedding.
        """
        te_out_dim = type_embedding.get_shape().as_list()[-1]
        self.test_type_embedding = type_embedding
        self.test_nei_embed = tf.nn.embedding_lookup(
            type_embedding, self.nei_type_vec
        )  # shape is [self.nnei, 1+te_out_dim]
        # nei_embed = tf.tile(nei_embed, (nframes * natoms[0], 1))  # shape is [nframes*natoms[0]*self.nnei, te_out_dim]
        nei_embed = tf.reshape(self.test_nei_embed, [-1, te_out_dim])
        self.embedding_input = tf.concat(
            [xyz_scatter, nei_embed], 1
        )  # shape is [nframes*natoms[0]*self.nnei, 1+te_out_dim]
        if not self.type_one_side:
            self.atm_embed = tf.nn.embedding_lookup(
                type_embedding, natype
            )  # shape is [nframes*natoms[0], te_out_dim]
            self.atm_embed = tf.tile(
                self.atm_embed, [1, self.nnei]
            )  # shape is [nframes*natoms[0], self.nnei*te_out_dim]
            self.atm_embed = tf.reshape(
                self.atm_embed, [-1, te_out_dim]
            )  # shape is [nframes*natoms[0]*self.nnei, te_out_dim]
            self.embedding_input_2 = tf.concat(
                [self.embedding_input, self.atm_embed], 1
            )  # shape is [nframes*natoms[0]*self.nnei, 1+te_out_dim+te_out_dim]
            return self.embedding_input_2
        return self.embedding_input

    def _feedforward(self, input_xyz, d_in, d_mid):
        residual = input_xyz
        input_xyz = tf.nn.relu(
            one_layer(
                input_xyz,
                d_mid,
                name="c_ffn1",
                reuse=tf.AUTO_REUSE,
                seed=self.seed,
                activation_fn=None,
                precision=self.filter_precision,
                trainable=True,
                uniform_seed=self.uniform_seed,
                initial_variables=self.attention_layer_variables,
            )
        )
        input_xyz = one_layer(
            input_xyz,
            d_in,
            name="c_ffn2",
            reuse=tf.AUTO_REUSE,
            seed=self.seed,
            activation_fn=None,
            precision=self.filter_precision,
            trainable=True,
            uniform_seed=self.uniform_seed,
            initial_variables=self.attention_layer_variables,
        )
        input_xyz += residual
        input_xyz = tf.keras.layers.LayerNormalization()(input_xyz)
        return input_xyz

    def _scaled_dot_attn(
        self,
        Q,
        K,
        V,
        temperature,
        input_r,
        dotr=False,
        do_mask=False,
        layer=0,
        save_weights=True,
    ):
        attn = tf.matmul(Q / temperature, K, transpose_b=True)
        attn *= self.nmask
        attn += self.negative_mask
        attn = tf.nn.softmax(attn, axis=-1)
        attn *= tf.reshape(self.nmask, [-1, attn.shape[-1], 1])
        if save_weights:
            self.attn_weight[layer] = attn[0]  # atom 0
        if dotr:
            angular_weight = tf.matmul(input_r, input_r, transpose_b=True)  # normalized
            attn *= angular_weight
            if save_weights:
                self.angular_weight[layer] = angular_weight[0]  # atom 0
                self.attn_weight_final[layer] = attn[0]  # atom 0
        if do_mask:
            nei = int(attn.shape[-1])
            mask = tf.cast(tf.ones((nei, nei)) - tf.eye(nei), self.filter_precision)
            attn *= mask
        output = tf.matmul(attn, V)
        return output

    def _attention_layers(
        self,
        input_xyz,
        layer_num,
        shape_i,
        outputs_size,
        input_r,
        dotr=False,
        do_mask=False,
        trainable=True,
        suffix="",
    ):
        sd_k = tf.sqrt(tf.cast(1.0, dtype=self.filter_precision))
        for i in range(layer_num):
            name = f"attention_layer_{i}{suffix}"
            with tf.variable_scope(name, reuse=tf.AUTO_REUSE):
                # input_xyz_in = tf.nn.l2_normalize(input_xyz, -1)
                Q_c = one_layer(
                    input_xyz,
                    self.att_n,
                    name="c_query",
                    scope=name + "/",
                    reuse=tf.AUTO_REUSE,
                    seed=self.seed,
                    activation_fn=None,
                    precision=self.filter_precision,
                    trainable=trainable,
                    uniform_seed=self.uniform_seed,
                    initial_variables=self.attention_layer_variables,
                )
                K_c = one_layer(
                    input_xyz,
                    self.att_n,
                    name="c_key",
                    scope=name + "/",
                    reuse=tf.AUTO_REUSE,
                    seed=self.seed,
                    activation_fn=None,
                    precision=self.filter_precision,
                    trainable=trainable,
                    uniform_seed=self.uniform_seed,
                    initial_variables=self.attention_layer_variables,
                )
                V_c = one_layer(
                    input_xyz,
                    self.att_n,
                    name="c_value",
                    scope=name + "/",
                    reuse=tf.AUTO_REUSE,
                    seed=self.seed,
                    activation_fn=None,
                    precision=self.filter_precision,
                    trainable=trainable,
                    uniform_seed=self.uniform_seed,
                    initial_variables=self.attention_layer_variables,
                )
                # # natom x nei_type_i x out_size
                # xyz_scatter = tf.reshape(xyz_scatter, (-1, shape_i[1] // 4, outputs_size[-1]))
                # natom x nei_type_i x att_n
                Q_c = tf.nn.l2_normalize(
                    tf.reshape(Q_c, (-1, shape_i[1] // 4, self.att_n)), -1
                )
                K_c = tf.nn.l2_normalize(
                    tf.reshape(K_c, (-1, shape_i[1] // 4, self.att_n)), -1
                )
                V_c = tf.nn.l2_normalize(
                    tf.reshape(V_c, (-1, shape_i[1] // 4, self.att_n)), -1
                )

                input_att = self._scaled_dot_attn(
                    Q_c, K_c, V_c, sd_k, input_r, dotr=dotr, do_mask=do_mask, layer=i
                )
                input_att = tf.reshape(input_att, (-1, self.att_n))

                # (natom x nei_type_i) x out_size
                input_xyz += one_layer(
                    input_att,
                    outputs_size[-1],
                    name="c_out",
                    scope=name + "/",
                    reuse=tf.AUTO_REUSE,
                    seed=self.seed,
                    activation_fn=None,
                    precision=self.filter_precision,
                    trainable=trainable,
                    uniform_seed=self.uniform_seed,
                    initial_variables=self.attention_layer_variables,
                )
                input_xyz = tf.keras.layers.LayerNormalization(
                    beta_initializer=tf.constant_initializer(self.beta[i]),
                    gamma_initializer=tf.constant_initializer(self.gamma[i]),
                )(input_xyz)
                # input_xyz = self._feedforward(input_xyz, outputs_size[-1], self.att_n)
        return input_xyz

    def _filter_lower(
        self,
        type_i,
        type_input,
        start_index,
        incrs_index,
        inputs,
        type_embedding=None,
        atype=None,
        is_exclude=False,
        activation_fn=None,
        bavg=0.0,
        stddev=1.0,
        trainable=True,
        suffix="",
        name="filter_",
        reuse=None,
    ):
        """input env matrix, returns R.G."""
        outputs_size = [1] + self.filter_neuron
        # cut-out inputs
        # with natom x (nei_type_i x 4)
        inputs_i = tf.slice(inputs, [0, start_index * 4], [-1, incrs_index * 4])
        shape_i = inputs_i.get_shape().as_list()
        natom = tf.shape(inputs_i)[0]
        # with (natom x nei_type_i) x 4
        inputs_reshape = tf.reshape(inputs_i, [-1, 4])
        # with (natom x nei_type_i) x 1
        xyz_scatter = tf.reshape(tf.slice(inputs_reshape, [0, 0], [-1, 1]), [-1, 1])
        assert atype is not None, 'atype must exist!!'
        type_embedding = tf.cast(type_embedding, self.filter_precision) # ntypes * Y
        #xyz_scatter = self._lookup_type_embedding(
        #    xyz_scatter, atype, type_embedding)
        if self.compress:
            raise RuntimeError(
                "compression of attention descriptor is not supported at the moment"
            )
        # natom x 4 x outputs_size
        if not is_exclude:
            with tf.variable_scope(name, reuse=reuse):
                # with (natom x nei_type_i) x out_size
                xyz_scatter = embedding_net(
                    xyz_scatter,
                    self.filter_neuron,
                    self.filter_precision,
                    activation_fn=activation_fn,
                    resnet_dt=self.filter_resnet_dt,
                    name_suffix=suffix,
                    stddev=stddev,
                    bavg=bavg,
                    seed=self.seed,
                    trainable=trainable,
                    uniform_seed=self.uniform_seed,
                    initial_variables=self.embedding_net_variables,
                    mixed_prec=self.mixed_prec)
                out_size = xyz_scatter.get_shape().as_list()[-1]
                #xyz_scatter = tf.nn.embedding_lookup(xyz_scatter,
                #                                     self.nei_type_vec)
                #xyz_scatter = tf.reshape(xyz_scatter, [-1, out_size])  # nframes*natoms[0] * nei * out_size

                if self.type_one_side:
                    embedding_of_embedding_suffix = suffix + "_ebd_of_ebd"
                    embedding_of_embedding = embedding_net(
                        type_embedding,
                        self.filter_neuron,
                        self.filter_precision,
                        activation_fn=activation_fn,
                        resnet_dt=self.filter_resnet_dt,
                        name_suffix=embedding_of_embedding_suffix,
                        stddev=stddev,
                        bavg=bavg,
                        seed=self.seed,
                        trainable=trainable,
                        uniform_seed=self.uniform_seed,
                        initial_variables=self.embedding_net_variables,
                        mixed_prec=self.mixed_prec)  # ntypes * out_size
                    nei_embed = tf.nn.embedding_lookup(embedding_of_embedding, self.nei_type_vec)
                    #nei_embed = tf.reshape(self.nei_embed, [-1, out_size])  # nframes*natoms[0] * nei * out_size

                    xyz_scatter = xyz_scatter + nei_embed
                else:
                    type_embedding_shape = type_embedding.get_shape().as_list()
                    type_embedding_nei = tf.tile(tf.reshape(type_embedding, [1, type_embedding_shape[0], -1]),
                                                 [type_embedding_shape[0], 1, 1])  # (ntypes) * ntypes * Y
                    type_embedding_center = tf.tile(tf.reshape(type_embedding, [type_embedding_shape[0], 1, -1]),
                                                    [1, type_embedding_shape[0], 1])  # ntypes * (ntypes) * Y
                    two_side_type_embedding = tf.concat([type_embedding_nei, type_embedding_center], -1) # ntypes * ntypes * (Y+Y)
                    two_side_type_embedding = tf.reshape(two_side_type_embedding, [-1, two_side_type_embedding.shape[-1]])
                    two_side_type_embedding_suffix = suffix + "_two_side_ebd" 
                    embedding_of_two_side_type_embedding = embedding_net(
                        two_side_type_embedding,
                        self.filter_neuron,
                        self.filter_precision,
                        activation_fn=activation_fn,
                        resnet_dt=self.filter_resnet_dt,
                        name_suffix=two_side_type_embedding_suffix,
                        stddev=stddev,
                        bavg=bavg,
                        seed=self.seed,
                        trainable=trainable,
                        uniform_seed=self.uniform_seed,
                        initial_variables=self.embedding_net_variables,
                        mixed_prec=self.mixed_prec)
                    #index_of_two_side = self.nei_type_vec * self.ntypes + tf.tile(atype, [1, self.nnei])
                    tmpres1 = self.nei_type_vec * type_embedding_shape[0]
                    tmpres2 = tf.tile(atype, [self.nnei])
                    index_of_two_side = tmpres1 + tmpres2
                    two_embd = tf.nn.embedding_lookup(embedding_of_two_side_type_embedding, index_of_two_side)

                    embedding_compose_s = tf.get_variable(
                        "embedding_compose_s",
                        [out_size],
                        dtype=GLOBAL_TF_FLOAT_PRECISION,
                        initializer=tf.random_normal_initializer(mean=0, stddev=1, seed=self.seed),
                        trainable=True,
                    )
                    embedding_compose_n = tf.get_variable(
                        "embedding_compose_n",
                        [out_size],
                        dtype=GLOBAL_TF_FLOAT_PRECISION,
                        initializer=tf.random_normal_initializer(mean=0, stddev=1, seed=self.seed),
                        trainable=True,
                    )
                    xyz_scatter = xyz_scatter * two_embd + embedding_compose_s * xyz_scatter + embedding_compose_n * two_embd

                if (not self.uniform_seed) and (self.seed is not None):
                    self.seed += self.seed_shift
            input_r = tf.slice(
                tf.reshape(inputs_i, (-1, shape_i[1] // 4, 4)), [0, 0, 1], [-1, -1, 3]
            )
            input_r = tf.nn.l2_normalize(input_r, -1)
            # natom x nei_type_i x out_size
            xyz_scatter_att = tf.reshape(
                self._attention_layers(
                    xyz_scatter,
                    self.attn_layer,
                    shape_i,
                    outputs_size,
                    input_r,
                    dotr=self.attn_dotr,
                    do_mask=self.attn_mask,
                    trainable=trainable,
                    suffix=suffix,
                ),
                (-1, shape_i[1] // 4, outputs_size[-1]),
            )
            # xyz_scatter = tf.reshape(xyz_scatter, (-1, shape_i[1] // 4, outputs_size[-1]))
        else:
            raise RuntimeError("this should not be touched")
        # When using tf.reshape(inputs_i, [-1, shape_i[1]//4, 4]) below
        # [588 24] -> [588 6 4] correct
        # but if sel is zero
        # [588 0] -> [147 0 4] incorrect; the correct one is [588 0 4]
        # So we need to explicitly assign the shape to tf.shape(inputs_i)[0] instead of -1
        return tf.matmul(
            tf.reshape(inputs_i, [natom, shape_i[1] // 4, 4]),
            xyz_scatter_att,
            transpose_a=True,
        )

    @cast_precision
    def _filter(
        self,
        inputs,
        type_input,
        natoms,
        type_embedding=None,
        atype=None,
        activation_fn=tf.nn.tanh,
        stddev=1.0,
        bavg=0.0,
        suffix="",
        name="linear",
        reuse=None,
        trainable=True,
    ):
        nframes = tf.shape(tf.reshape(inputs, [-1, natoms[0], self.ndescrpt]))[0]
        # natom x (nei x 4)
        shape = inputs.get_shape().as_list()
        outputs_size = [1] + self.filter_neuron
        outputs_size_2 = self.n_axis_neuron

        start_index = 0
        type_i = 0
        # natom x 4 x outputs_size
        xyz_scatter_1 = self._filter_lower(
            type_i,
            type_input,
            start_index,
            np.cumsum(self.sel_a)[-1],
            inputs,
            type_embedding=type_embedding,
            is_exclude=False,
            activation_fn=activation_fn,
            stddev=stddev,
            bavg=bavg,
            trainable=trainable,
            suffix=suffix,
            name=name,
            reuse=reuse,
            atype=atype,
        )
        # natom x nei x outputs_size
        # xyz_scatter = tf.concat(xyz_scatter_total, axis=1)
        # natom x nei x 4
        # inputs_reshape = tf.reshape(inputs, [-1, shape[1]//4, 4])
        # natom x 4 x outputs_size
        # xyz_scatter_1 = tf.matmul(inputs_reshape, xyz_scatter, transpose_a = True)
        if self.original_sel is None:
            # shape[1] = nnei * 4
            nnei = shape[1] / 4
        else:
            nnei = tf.cast(
                tf.Variable(
                    np.sum(self.original_sel),
                    dtype=tf.int32,
                    trainable=False,
                    name="nnei",
                ),
                self.filter_precision,
            )
        xyz_scatter_1 = xyz_scatter_1 / nnei
        # natom x 4 x outputs_size_2
        xyz_scatter_2 = tf.slice(xyz_scatter_1, [0, 0, 0], [-1, -1, outputs_size_2])
        # # natom x 3 x outputs_size_2
        # qmat = tf.slice(xyz_scatter_2, [0,1,0], [-1, 3, -1])
        # natom x 3 x outputs_size_1
        qmat = tf.slice(xyz_scatter_1, [0, 1, 0], [-1, 3, -1])
        # natom x outputs_size_1 x 3
        qmat = tf.transpose(qmat, perm=[0, 2, 1])
        # natom x outputs_size x outputs_size_2
        result = tf.matmul(xyz_scatter_1, xyz_scatter_2, transpose_a=True)
        # natom x (outputs_size x outputs_size_2)
        result = tf.reshape(result, [-1, outputs_size_2 * outputs_size[-1]])

        return result, qmat

    def init_variables(
        self,
        graph: tf.Graph,
        graph_def: tf.GraphDef,
        suffix: str = "",
    ) -> None:
        """Init the embedding net variables with the given dict.

        Parameters
        ----------
        graph : tf.Graph
            The input frozen model graph
        graph_def : tf.GraphDef
            The input frozen model graph_def
        suffix : str, optional
            The suffix of the scope
        """
        super().init_variables(graph=graph, graph_def=graph_def, suffix=suffix)
        self.attention_layer_variables = get_attention_layer_variables_from_graph_def(
            graph_def, suffix=suffix
        )
        if self.attn_layer > 0:
            self.beta[0] = self.attention_layer_variables[
                f"attention_layer_0{suffix}/layer_normalization/beta"
            ]
            self.gamma[0] = self.attention_layer_variables[
                f"attention_layer_0{suffix}/layer_normalization/gamma"
            ]
            for i in range(1, self.attn_layer):
                self.beta[i] = self.attention_layer_variables[
                    "attention_layer_{}{}/layer_normalization_{}/beta".format(
                        i, suffix, i
                    )
                ]
                self.gamma[i] = self.attention_layer_variables[
                    "attention_layer_{}{}/layer_normalization_{}/gamma".format(
                        i, suffix, i
                    )
                ]

    def build_type_exclude_mask(
        self,
        exclude_types: List[Tuple[int, int]],
        ntypes: int,
        sel: List[int],
        ndescrpt: int,
        atype: tf.Tensor,
        shape0: tf.Tensor,
        nei_type_vec: tf.Tensor,
    ) -> tf.Tensor:
        r"""Build the type exclude mask for the attention descriptor.

        Notes
        -----
        This method has the similiar way to build the type exclude mask as
        :meth:`deepmd.descriptor.descriptor.Descriptor.build_type_exclude_mask`.
        The mathmatical expression has been explained in that method.
        The difference is that the attention descriptor has provided the type of
        the neighbors (idx_j) that is not in order, so we use it from an extra
        input.

        Parameters
        ----------
        exclude_types : List[Tuple[int, int]]
            The list of excluded types, e.g. [(0, 1), (1, 0)] means the interaction
            between type 0 and type 1 is excluded.
        ntypes : int
            The number of types.
        sel : List[int]
            The list of the number of selected neighbors for each type.
        ndescrpt : int
            The number of descriptors for each atom.
        atype : tf.Tensor
            The type of atoms, with the size of shape0.
        shape0 : tf.Tensor
            The shape of the first dimension of the inputs, which is equal to
            nsamples * natoms.
        nei_type_vec : tf.Tensor
            The type of neighbors, with the size of (shape0, nnei).

        Returns
        -------
        tf.Tensor
            The type exclude mask, with the shape of (shape0, ndescrpt), and the
            precision of GLOBAL_TF_FLOAT_PRECISION. The mask has the value of 1 if the
            interaction between two types is not excluded, and 0 otherwise.

        See Also
        --------
        deepmd.descriptor.descriptor.Descriptor.build_type_exclude_mask
        """
        # generate a mask
        # op returns ntypes when the neighbor doesn't exist, so we need to add 1
        type_mask = np.array(
            [
                [
                    1 if (tt_i, tt_j) not in exclude_types else 0
                    for tt_i in range(ntypes + 1)
                ]
                for tt_j in range(ntypes)
            ],
            dtype=bool,
        )
        type_mask = tf.convert_to_tensor(type_mask, dtype=GLOBAL_TF_FLOAT_PRECISION)
        type_mask = tf.reshape(type_mask, [-1])

        # (nsamples * natoms, 1)
        atype_expand = tf.reshape(atype, [-1, 1])
        # (nsamples * natoms, ndescrpt)
        idx_i = tf.tile(atype_expand * (ntypes + 1), (1, ndescrpt))
        # idx_j has been provided by atten op
        # (nsamples * natoms, nnei, 1)
        idx_j = tf.reshape(nei_type_vec, [shape0, sel[0], 1])
        # (nsamples * natoms, nnei, ndescrpt // nnei)
        idx_j = tf.tile(idx_j, (1, 1, ndescrpt // sel[0]))
        # (nsamples * natoms, ndescrpt)
        idx_j = tf.reshape(idx_j, [shape0, ndescrpt])
        idx = idx_i + idx_j
        idx = tf.reshape(idx, [-1])
        mask = tf.nn.embedding_lookup(type_mask, idx)
        # same as inputs_i, (nsamples * natoms, ndescrpt)
        mask = tf.reshape(mask, [-1, ndescrpt])
        return mask
