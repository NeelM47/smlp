from fractions import Fraction
import json

import smlp
from smlp_py.smlp_terms import ModelTerms, SmlpTerms
from smlp_py.smlp_utils import np_JSONEncoder

class SmlpVerify:
    def __init__(self):
        self._smlpTermsInst = SmlpTerms()
        self._modelTermsInst = None #ModelTerms()
        
        self._DEF_ASSERTIONS_NAMES = None
        self._DEF_ASSERTIONS_EXPRS = None
        
        self.verify_params_dict = {
            'assertions_names': {'abbr':'asrt_names', 'default':str(self._DEF_ASSERTIONS_NAMES), 'type':str,
                'help':'Names of optimization objectives [default {}]'.format(str(self._DEF_ASSERTIONS_NAMES))}, 
            'assertions_expressions':{'abbr':'asrt_exprs', 'default':self._DEF_ASSERTIONS_EXPRS, 'type':str,
                'help':'Semicolon seperated list of expressions (functions) to be applied to the responses '
                    'to convert them into optimization objectives ' +
                    '[default: {}]'.format(str(self._DEF_ASSERTIONS_EXPRS))}
        }
        
    def set_logger(self, logger):
        self._verify_logger = logger 
        self._smlpTermsInst.set_logger(logger)
        self._modelTermsInst.set_logger(logger)
    
    # report_file_prefix is a string used as prefix in all report files of SMLP
    def set_report_file_prefix(self, report_file_prefix):
        self.report_file_prefix = report_file_prefix
        self._modelTermsInst.set_report_file_prefix(report_file_prefix)
        
    # model_file_prefix is a string used as prefix in all saved model files of SMLP
    def set_model_file_prefix(self, model_file_prefix):
        self.model_file_prefix = model_file_prefix
        self._modelTermsInst.set_model_file_prefix(model_file_prefix)
    
    # set self._modelTermsInst ModelTerms()
    def set_model_terms_inst(self, model_terms_inst):
        self._modelTermsInst = model_terms_inst
    
    @property
    def assertions_results_file(self):
        assert self.report_file_prefix is not None
        return self.report_file_prefix + '_assertions_results.json'
    
    def get_assertions(self, arg_asrt_names, arg_asrt_exprs, commandline_condition_separator):
        if arg_asrt_exprs is None:
            return None, None
        else:
            asrt_exprs = arg_asrt_exprs.split(commandline_condition_separator)
            if arg_asrt_names is not None:
                asrt_names = arg_asrt_names.split(',')
            else:
                asrt_names = ['asrt_'+str(i) for i in enumerate(len(asrt_exprs))];
        assert asrt_names is not None and asrt_exprs is not None
        assert len(asrt_names) == len(asrt_exprs); 
        print('asrt_names', asrt_names); print('asrt_exprs', asrt_exprs)
        return asrt_names, asrt_exprs

    def verify_asrt(self, solver_instance:smlp.libsmlp.solver, asrt_name:str, asrt_expr:str, asrt_form:smlp.form2, 
            alpha:smlp.form2, beta:smlp.form2, eta:smlp.form2, sat_approx:bool, sat_precision:int):
        self._verify_logger.info('Verifying assertion {} <-> {}'.format(str(asrt_name), str(asrt_expr)))
        # TODO !!!: take care of usage of beta
        assert beta == smlp.true

        solver_instance.add(alpha)
        #solver_instance.add(eta)
        solver_instance.add(self._smlpTermsInst.smlp_not(asrt_form))
        res = solver_instance.check(); #self.print_result(res)
        
        if isinstance(res, smlp.unsat):
            self._verify_logger.info('Completed with result: {}'.format('UNSAT'))
            asrt_res_dict = {'status':'UNSAT', 'asrt':None, 'model':None}
        elif isinstance(res, smlp.sat):
            self._verify_logger.info('Completed with result: {}'.format('SAT'))
            print('res/model', res.model, type(res.model), type(res))
            sat_model_vals_dict = self._smlpTermsInst.sat_model_term_to_const(res.model, 
                approximate=sat_approx, precision=sat_precision)
            print('domain sat_model_vals_dict', sat_model_vals_dict)
            # sanity check: the value of the negated assertion in the sat assignment should be true
            asrt_ce_val = eval(asrt_expr, {},  sat_model_vals_dict); print('asrt_ce_val', asrt_ce_val)
            assert not asrt_ce_val
            asrt_res_dict = {'status':'SAT', 'asrt': asrt_ce_val, 'model':sat_model_vals_dict}
        elif isinstance(res, smlp.unknown):
            self._verify_logger.info('Completed with result: {}'.format('UNKNOWN'))
            # TODO !!!: add reason for UNKNOWN or report that reason as 'status' field
            asrt_res_dict = {'status':'UNKNOWN', 'asrt':None, 'model':None}
        else:
            raise Exception('Unexpected resuld from solver')
        return asrt_res_dict
        
        
    def verify_assertions(self, solver_instance:smlp.libsmlp.solver, asrt_names:list, asrt_exprs:list, asrt_forms_dict:dict, 
            alpha:smlp.form2, beta:smlp.form2, eta:smlp.form2, sat_approx=False, sat_precision=64):
        print('asrt_forms_dict', asrt_forms_dict)
        assert list(asrt_forms_dict.keys()) == asrt_names
        asrt_res_dict = {}
        for i, (asrt_name, asrt_form) in enumerate(asrt_forms_dict.items()):
            asrt_res_dict[asrt_name] = self.verify_asrt(solver_instance, asrt_name, asrt_exprs[i], asrt_form, 
                alpha, beta, eta, sat_approx, sat_precision)
        print('asrt_res_dict', asrt_res_dict)
        with open(self.assertions_results_file, 'w') as f: #json.dump(asrt_res_dict, f)
            json.dump(asrt_res_dict, f, indent='\t', cls=np_JSONEncoder) #cls= , use_decimal=True
            
    def smlp_verify(self, algo, model, X, y, model_features_dict, feat_names, resp_names, 
            objv_names, objv_exprs, pareto, asrt_names, asrt_exprs, quer_names, quer_exprs, delta, epsilon,
            alph_expr:str, beta_expr:str, eta_expr:str, data_scaler, scale_feat, scale_resp, scale_objv, 
            float_approx=True, float_precision=64, data_bounds_json_path=None, bounds_factor=None, T_resp_bounds_csv_path=None):
        domain, model_full_term_dict, eta, alpha, beta, base_solver = self._modelTermsInst.create_model_exploration_base_instance( #spec, 
            algo, model, X, y, model_features_dict, feat_names, resp_names, 
            objv_names, objv_exprs, asrt_names, asrt_exprs, quer_names, quer_exprs, delta, epsilon,
            alph_expr, beta_expr, eta_expr, True, data_scaler, scale_feat, scale_resp, scale_objv, 
            float_approx, float_precision, data_bounds_json_path)
        print('eta, alpha, beta', eta, alpha, beta)
        asrt_forms_dict = dict([(asrt_name, self._smlpTermsInst.ast_expr_to_term(asrt_expr)) \
                for asrt_name, asrt_expr in zip(asrt_names, asrt_exprs)])
        for i, form in enumerate(asrt_forms_dict.values()):
            if not isinstance(form, smlp.libsmlp.form2):
                raise Exception('Assertion ' + str(asrt_exprs[i]) + ' must be a formula (not a term)')
        self.verify_assertions(base_solver, asrt_names, asrt_exprs, asrt_forms_dict, alpha, beta, eta, float_approx, float_precision)

