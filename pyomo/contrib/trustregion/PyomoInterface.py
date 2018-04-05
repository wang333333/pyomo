import os
import numpy as np
from pyutilib.math import infinity
from pyutilib.services import TempfileManager
from pyomo.util.modeling import randint, unique_component_name
from pyomo.core import Block, Var, Param, Set, VarList, ConstraintList, Constraint, Objective, RangeSet, value, ConcreteModel, Reals, sqrt, minimize, maximize
from pyomo.core.base import expr_coopr3, expr as EXPR
from pyomo.core.base.var import _VarData
from pyomo.core.base.numvalue import native_types
from pyomo.opt import SolverFactory, SolverStatus, TerminationCondition
from pyomo.contrib.trustregion.readgjh import *
from pyomo.contrib.trustregion.helper import *

GEO_DIR = os.path.join(os.path.dirname(__file__), 'QradROMGeo')

class ROMType:
    linear = 0
    quadratic = 1

class PyomoInterface:
    '''
    Initialize with a pyomo model m.
    This is used in TRF.py, same requirements for m apply

    m is reformulated into form for use in TRF algorithm

    Specified ExternalFunction() objects are replaced with new variables
    All new attributes (including these variables) are stored on block
    "tR"


    Note: quadratic ROM is messy, uses full dimension of x variables. clean up later.

    '''

    TempfileManager.tempdir = "."
    solver = 'ipopt'
    solver_io = 'nl'
    stream_solver = False # True prints solver output to screen
    keepfiles = False  # True prints intermediate file names (.nl,.sol,...)
    countDx = -1
    romtype = ROMType.linear

    def __init__(self, m, eflist):

        self.model = m;
        self.TRF = self.transformForTrustRegion(self.model,eflist)

        self.lx = len(self.TRF.xvars)
        self.lz = len(self.TRF.zvars)
        self.ly = len(self.TRF.y)

        self.createParam()
        self.createRomConstraint()
        self.createCompCheckObjective()
        self.cacheBound()



    def substituteEF(self,expr,trf,efSet):
    # Substitute out an External Function
    #
    # Arguments:
    # expr : a pyomo expression. We will search this expression tree
    # trf : a pyomo block. We will add tear variables y on this block
    # efSet: the (pyomo) set of external functions for which we will use TRF method
    #
    # This function returns an expression after removing any ExternalFunction
    # in the set efSet from the expression tree expr. New variables are declared on
    # the trf block and replace the external function.

    ## Recursive calls of external function won't work, but won't raise an exception!!!!!!!

        stack = [(None, [expr], 0, 1)]
        while stack:
            _node, _args, _idx, _len = stack.pop()
            while _idx < _len:

                _sub = _args[_idx]
                _idx += 1
                if type(_sub) in native_types:
                    pass
                elif type(_sub) is EXPR._ExternalFunctionExpression and id(_sub._fcn) in efSet:
                    _y = trf.y.add()
                    trf.external_fcns.append(_sub)
                    seen = set()
                    ans = []
                    # starts at 1, _sub._args[0] is ignored for some reason?!
                    for _arg in _sub._args[1:]:
                        if _arg.is_expression():
                            # If one arg is an expression, a new variable is created and a new constraint is added
                            _x = trf.x.add()
                            _x.set_value(value(_arg))
                            trf.conset.add(_x==_arg)
                            ans.append(_x)
                        elif isinstance(_arg, _VarData):
                            if id(_arg) not in seen:
                                seen.add(id(_arg))
                                ans.append(_arg)
                            else:
                                raise Exception("We do not support a black box with duplicate input arguments\n")
                        else:
                            #TODO: Support constants and paramters here!
                            raise Exception("We do not support a black box with constant or parameter inputs\n")

                    trf.exfn_xvars.append(ans)

                    if type(_args) is list:
                        # This covers the special case of _ProductExpression's numerator/denominator
                        _args[_idx-1] = _y
                    else: # this means _args is a tuple!
                        try:
                            _node._args = _args[:_idx-1] + (_y,) + _args[_idx:]
                        except AttributeError:
                            if type(_node._expr) is EXPR._ExternalFunctionExpression:
                                _node._expr = _y
                            else:
                                raise AttributeError("Hacky code doesn't work!!")
                elif _sub.is_expression():
                    stack.append((_node, _args, _idx, _len))
                    if type(_sub) is expr_coopr3._ProductExpression:
                        stack.append((_sub, _sub._denominator, 0, len(_sub._denominator)))
                        _args = _sub._numerator
                    else:
                        _args = _sub._args
                    _idx = 0
                    _len = len(_args)
                    _node = _sub

        return _args[0]

    def transformForTrustRegion(self,model,eflist):
        # transform and model into suitable form for TRF method
        #
        # Arguments:
        # model : pyomo model containing ExternalFunctions
        # eflist : a list of the external functions that will be
        #   handled with TRF method rather than calls to compiled code

        efSet = set([id(x) for x in eflist])

        TRF = Block()

        # Get all varibles
        seenVar = Set()
        allVariables = []
        for var in model.component_data_objects(Var):
            if id(var) not in seenVar:
                seenVar.add(id(var))
                allVariables.append(var)


        # This assumes that an external funtion call is present, required!
        model.add_component(unique_component_name(model,'tR'), TRF)
        TRF.y = VarList()
        TRF.x = VarList()
        TRF.conset = ConstraintList()
        TRF.external_fcns = []
        TRF.exfn_xvars = []

        # TODO: Copy constraints onto block so that transformation can be reversed.

        for con in model.component_data_objects(Constraint,active=True):
            con.set_value((con.lower, self.substituteEF(con.body,TRF,efSet), con.upper))
        for obj in model.component_data_objects(Objective,active=True):
            obj.set_value(self.substituteEF(obj.expr,TRF,efSet))
            ## Assume only one ative objective function here
            self.objective=obj


        if self.objective.sense == maximize:
            self.objective.expr = -1* self.objective.expr
            self.objective.sense = minimize



        # xvars and zvars are lists of x and z varibles as in the paper
        TRF.xvars = []
        TRF.zvars = []
        seenVar = Set()
        for varss in TRF.exfn_xvars:
            for var in varss:
                if id(var) not in seenVar:
                    seenVar.add(id(var))
                    TRF.xvars.append(var)

        for var in allVariables:
            if id(var) not in seenVar:
                seenVar.add(id(var))
                TRF.zvars.append(var)

        # TODO: build dict for exfn_xvars
        # assume it is not bottleneck of the code
        self.exfn_xvars_ind = []
        for varss in TRF.exfn_xvars:
            listtmp = []
            for var in varss:
                for i in range(len(TRF.xvars)):
                    if(id(var)==id(TRF.xvars[i])):
                        listtmp.append(i)
                        break

            self.exfn_xvars_ind.append(listtmp)

        return TRF

    # TODO:
    # def reverseTransform(self):
    #     # After solving the problem, return the
    #     # model back to the original form, and delete
    #     # all add-on structures
    #     for conobj in self.TRF.changed_objects:
    #         conobj.activate()

    #     self.model.del_component(self.model.tR)


    def getInitialValue(self):
        x = np.zeros(self.lx, dtype=float)
        y = np.zeros(self.ly, dtype=float)
        z = np.zeros(self.lz, dtype=float)
        for i in range(0, self.lx):
            x[i] = value(self.TRF.xvars[i])
        for i in range(0, self.ly):
            #initialization of y?
            y[i] = 1
        for i in range(0, self.lz):
            z[i] = value(self.TRF.zvars[i])
        return x, y, z

    def createParam(self):
        self.TRF.ind_lx=RangeSet(0,self.lx-1)
        self.TRF.ind_ly=RangeSet(0,self.ly-1)
        self.TRF.ind_lz=RangeSet(0,self.lz-1)
        self.TRF.px0 = Param(self.TRF.ind_lx,mutable=True,default=0)
        self.TRF.py0 = Param(self.TRF.ind_ly,mutable=True,default=0)
        self.TRF.pz0 = Param(self.TRF.ind_lz,mutable=True,default=0)
        self.TRF.plrom = Param(self.TRF.ind_ly,range(self.lx+1),mutable=True,default=0)
        self.TRF.pqrom = Param(self.TRF.ind_ly,range(int((self.lx*self.lx+self.lx*3)/2. + 1)),mutable=True,default=0)
        self.TRF.ppenaltyComp = Param(mutable=True,default=0)


    def ROMlinear(self,model,i):
        ind = self.exfn_xvars_ind[i]
        e1 = (model.plrom[i,0] + sum(model.plrom[i,j+1] * (model.xvars[ind[j]] - model.px0[ind[j]]) for j in range(0, len(ind))))
        return e1

    def ROMQuad(self,model,i):
        e1 = model.pqrom[i,0] + sum(model.pqrom[i,j+1] * (model.xvars[j] - model.px0[j]) for j in range(0,self.lx))
        count = self.lx+1
        for j1 in range(self.lx):
            for j2 in range(j1,self.lx):
                e1 += (model.xvars[j2] - model.px0[j2]) * (model.xvars[j1] - model.px0[j1])*model.pqrom[i,count]
                count = count + 1
        return e1


    def createRomConstraint(self):
        def consROMl(model, i):
            return  model.y[i+1] == self.ROMlinear(model,i)
        self.TRF.romL = Constraint(self.TRF.ind_ly, rule=consROMl)

        def consROMq(model, i):
            return  model.y[i+1] == self.ROMQuad(model,i)
        self.TRF.romQ = Constraint(self.TRF.ind_ly, rule=consROMq)

    def createCompCheckObjective(self):
        obj = 0
        model = self.TRF
        for i in range(0, self.ly):
            obj += (self.ROMlinear(model, i) - model.y[i+1]) ** 2
        # for i in range(0, self.lx):
        #     obj += model.ppenaltyComp * (model.xvars[i] - model.px0[i]) ** 2
        # for i in range(0, self.lz):
        #     obj += model.ppenaltyComp * (model.zvars[i] - model.pz0[i]) ** 2
        model.objCompCheckL = Objective(expr=obj)

        obj = 0
        for i in range(0, self.ly):
            obj += (self.ROMQuad(model, i) - model.y[i+1]) ** 2
        # for i in range(0, self.lx):
        #     obj += model.ppenaltyComp * (model.xvars[i] - model.px0[i]) ** 2
        # for i in range(0, self.lz):
        #     obj += model.ppenaltyComp * (model.zvars[i] - model.pz0[i]) ** 2
        model.objCompCheckQ = Objective(expr=obj)


    def cacheBound(self):
        self.TRF.xvarlo = []
        self.TRF.xvarup = []
        self.TRF.zvarlo = []
        self.TRF.zvarup = []
        for x in self.TRF.xvars:
            self.TRF.xvarlo.append(x.lb)
            self.TRF.xvarup.append(x.ub)
        for z in self.TRF.zvars:
            self.TRF.zvarlo.append(z.lb)
            self.TRF.zvarup.append(z.ub)


    def setParam(self,x0=None,y0=None,z0=None,rom_params=None, penaltyComp = None):
        if x0 is not None:
            for i in range(self.lx):
                self.TRF.px0[i] = x0[i]

        # if y0 is not None:
        #     for i in range(self.ly):
        #         self.TRF.py0[i] = y0[i]

        # if z0 is not None:
        #     for i in range(self.lz):
        #         self.TRF.pz0[i] = z0[i]

        if rom_params is not None:
            if(self.romtype==ROMType.linear):
                for i in range(self.ly):
                    for j in range(len(rom_params[i])):
                        self.TRF.plrom[i,j] = rom_params[i][j]
            elif(self.romtype==ROMType.quadratic):
                for i in range(self.ly):
                    for j in range(len(rom_params[i])):
                        self.TRF.pqrom[i,j] = rom_params[i][j]

        # if penaltyComp is not None:
        #     self.TRF.ppenaltyComp.set_value(penaltyComp)

    def setVarValue(self, x=None, y=None, z=None):
        if x is not None:
            if(len(x) != self.lx):
                raise Exception(
                    "setValue: The dimension of x is not consistant!\n")
            for i in range(0, self.lx):
                self.TRF.xvars[i].set_value(x[i])
        if y is not None:
            if(len(y) != self.ly):
                raise Exception(
                    "setValue: The dimension of y is not consistant!\n")
            for i in range(0, self.ly):
                self.TRF.y[i+1].set_value(y[i])

        if z is not None:
            if(len(z) != self.lz):
                raise Exception(
                    "setValue: The dimension of z is not consistant!\n")
            for i in range(0, self.lz):
                self.TRF.zvars[i].set_value(z[i])

    def setBound(self, x0, y0, z0, radius):
        for i in range(0,self.lx):
            self.TRF.xvars[i].setlb(maxIgnoreNone(x0[i] - radius,self.TRF.xvarlo[i]))
            self.TRF.xvars[i].setub(minIgnoreNone(x0[i] + radius,self.TRF.xvarup[i]))
        for i in range(0,self.ly):
            self.TRF.y[i+1].setlb(y0[i] - radius)
            self.TRF.y[i+1].setub(y0[i] + radius)
        for i in range(0,self.lz):
            self.TRF.zvars[i].setlb(maxIgnoreNone(z0[i] - radius,self.TRF.zvarlo[i]))
            self.TRF.zvars[i].setub(minIgnoreNone(z0[i] + radius,self.TRF.zvarup[i]))


    def evaluateDx(self,x):
        # This is messy, currently redundant with
        # some lines in buildROM()
        self.countDx += 1
        ans = []
        for i in range(0,self.ly):
            fcn = self.TRF.external_fcns[i]._fcn
            values = []
            for j in self.exfn_xvars_ind[i]:
                values.append(x[j])

            ans.append(fcn._fcn(*values))
        return np.array(ans)

    def evaluateObj(self, x, y, z):
        if(len(x) != self.lx or len(y) != self.ly or len(z) != self.lz):
            raise Exception("evaluateObj: The dimension is not consistent with the initialization \n")
        self.setVarValue(x=x,y=y,z=z)
        return self.objective()

    def deactiveExtraConObj(self):
        self.TRF.objCompCheckL.deactivate()
        self.TRF.romL.deactivate()
        self.TRF.objCompCheckQ.deactivate()
        self.TRF.romQ.deactivate()
        self.objective.activate()

    def activateRomCons(self,x0, rom_params):
        self.setParam(x0=x0,rom_params=rom_params)
        if(self.romtype==ROMType.linear):
            self.TRF.romL.activate()
        elif(self.romtype==ROMType.quadratic):
            self.TRF.romQ.activate()

    def activateCompCheckObjective(self, x0, z0, rom_params, penalty):
        self.setParam(x0=x0,z0=z0,rom_params=rom_params,penaltyComp = penalty)
        if(self.romtype==ROMType.linear):
            self.TRF.objCompCheckL.activate()
        elif(self.romtype==ROMType.quadratic):
            self.TRF.objCompCheckQ.activate()
        self.objective.deactivate()



    def solveModel(self, x, y, z):
        model = self.model
        opt = SolverFactory(self.solver, solver_io=self.solver_io)
        opt.options['halt_on_ampl_error'] = 'yes'
        opt.options['max_iter'] = 5000

        results = opt.solve(
            model, keepfiles=self.keepfiles, tee=self.stream_solver)

        if ((results.solver.status == SolverStatus.ok)
                and (results.solver.termination_condition == TerminationCondition.optimal)):
            model.solutions.load_from(results)
            for i in range(0, self.lx):
                x[i] = value(self.TRF.xvars[i])
            for i in range(0, self.ly):
                y[i] = value(self.TRF.y[i+1])
            for i in range(0, self.lz):
                z[i] = value(self.TRF.zvars[i])

            for obj in model.component_data_objects(Objective,active=True):
                return True, obj()

        else:
            print("Waring: solver Status: " + str(results.solver.status))
            print("And Termination Conditions: " + str(results.solver.termination_condition))
            return False, 0

    def TRSPk(self, x, y, z, x0, y0, z0, rom_params, radius):

        if(len(x) != self.lx or len(y) != self.ly or len(z) != self.lz or
                len(x0) != self.lx or len(y0) != self.ly or len(z0) != self.lz):
            raise Exception(
                "TRSP_k: The dimension is not consistant with the initialization!\n")

        self.setBound(x0, y0, z0, radius)
        self.setVarValue(x, y, z)
        self.deactiveExtraConObj()
        self.activateRomCons(x0, rom_params)

        return self.solveModel(x, y, z)

    def compatibilityCheck(self, x, y, z, x0, y0, z0, rom_params, radius, penalty):
        if(len(x) != self.lx or len(y) != self.ly or len(z) != self.lz or
                len(x0) != self.lx or len(y0) != self.ly or len(z0) != self.lz):
            raise Exception(
                "Compatibility_Check: The dimension is not consistant with the initialization!\n")

        self.setBound(x0, y0, z0, radius)
        self.setVarValue(x, y, z)
        self.deactiveExtraConObj()
        self.activateCompCheckObjective(x0, z0, rom_params, penalty)
        #self.deactiveExtraConObj()
        #self.model.pprint()
        return self.solveModel(x, y, z)

    def criticalityCheck(self, x, y, z, rom_params, worstcase=False, M=[0.0]):

        model = self.model

        self.setVarValue(x=x,y=y,z=z)
        self.setBound(x, y, z, 1e10)
        self.deactiveExtraConObj()
        self.activateRomCons(x, rom_params)

        optGJH = SolverFactory('gjh', solver_io='nl')

        optGJH.solve(
            model, keepfiles=True, tee=False, symbolic_solver_labels=True)

        g, J, varlist, conlist = readgjh()

        l = ConcreteModel()
        l.v = Var(varlist, domain=Reals)
        for i in varlist:
            #dummy = model.find_component(i)
            l.v[i] = 0.0
            l.v[i].setlb(-1.0)
            l.v[i].setub(1.0)
        if worstcase:
            if M.all() == 0.0:
                print('WARNING: worstcase criticality was requested but Jacobian error bound is zero')
            l.t = Var(range(0, self.ly), domain=Reals)
            for i in range(0, self.ly):
                l.t[i].setlb(-M[i])
                l.t[i].setub(M[i])

        def linConMaker(l, i):
            # i should be range(len(conlist) - 1)
            # because last element of conlist is the objective
            con_i = model.find_component(conlist[i])

            isEquality = con_i.equality

            isROM = False

            if conlist[i][:7] == '.' + self.TRF.name + '.rom':
                isROM = True
                romIndex = int(filter(str.isdigit, conlist[i]))

            # This is very inefficient
            # Fix this later if these problems get big
            # This is the ith row of J times v
            Jv = sum(x[2] * l.v[varlist[x[1]]] for x in J if x[0] == i)

            if isEquality:
                if worstcase and isROM:
                    return Jv + l.t[romIndex] == 0
                else:
                    return Jv == 0
            else:
                lo = con_i.lower
                up = con_i.upper
                if lo is not None:
                    level = lo.value - con_i.lslack()
                    if up is not None:
                        return (lo.value <= level + Jv <= up.value)
                    else:
                        return (lo.value <= level + Jv)
                elif up is not None:
                    level = up.value - con_i.uslack()
                    return (level + Jv <= up.value)
                else:
                    raise Exception(
                        "This constraint seems to be neither equality or inequality: " + conlist(i))


        l.lincons = Constraint(range(len(conlist)-1), rule=linConMaker)

        l.obj = Objective(expr=sum(x[1] * l.v[varlist[x[0]]] for x in g))

        # Calculate gradient norm for scaling purposes
        gfnorm = sqrt(sum(x[1]**2 for x in g))


        opt = SolverFactory(self.solver, solver_io=self.solver_io)
        opt.options['halt_on_ampl_error'] = 'yes'
        opt.options['max_iter'] = 5000
        results = opt.solve(
            l, keepfiles=self.keepfiles, tee=self.stream_solver)

        if ((results.solver.status == SolverStatus.ok)
                and (results.solver.termination_condition == TerminationCondition.optimal)):
            l.solutions.load_from(results)
            if gfnorm > 1:
                return True, abs(l.obj())/gfnorm
            else:
                return True, abs(l.obj())
        else:
            print("Waring: Crticality check fails with solver Status: " + str(results.solver.status))
            print("And Termination Conditions: " + str(results.solver.termination_condition))
            return False, infinity




    ####################### Build ROM ####################
    geoM = None
    pset = None

    def initialQuad(self, lx):
        psetOpt = np.loadtxt(os.path.join(GEO_DIR, 'geo%d.out'%lx))
        mat = []
        for p in psetOpt:
            basisValue = [1]
            for i1 in range(0, lx):
                basisValue.append(p[i1])
            for i1 in range(0, lx):
                for i2 in range(i1, lx):
                    basisValue.append(p[i1]*p[i2])
            mat.append(basisValue)
        self.geoM = np.matrix(mat)
        self.pset = psetOpt

    def buildROM(self, x, radius_base):
        """
        This function builds a linear ROM near x based on the perturbation.
        The ROM is returned by a format of params array.
        I think the evaluate count is broken here!
        """

        y1 = self.evaluateDx(x)
        rom_params = []

        if(self.romtype==ROMType.linear):
            for i in range(0, self.ly):
                rom_params.append([])
                rom_params[i].append(y1[i])

                # Check if it works with Ampl
                fcn  =  self.TRF.external_fcns[i]._fcn
                values = [];
                for j in self.exfn_xvars_ind[i]:
                    values.append(x[j])

                for j in range(0, len(values)):
                    radius = radius_base # * scale[j]
                    values[j] = values[j] + radius
                    y2 = fcn._fcn(*values)
                    rom_params[i].append((y2 - y1[i]) / radius)
                    values[j] = values[j] - radius

        elif(self.romtype==ROMType.quadratic):
            #Quad ROM
            # basis = [1, x1, x2,..., xn, x1x1, x1x2,x1x3,...,x1xn,x2x2,x2x3,...,xnxn]
            if self.geoM is None:
                self.initialQuad(self.lx)

            dim =  (self.lx*self.lx+self.lx*3)/2 + 1
            rhs=[]
            radius = radius_base #*np.array(scale)
            for p in self.pset[:-1]:
                y = self.evaluateDx(x+radius*p)
                rhs.append(y)
            rhs.append(y1)

            coefs = np.linalg.solve(self.geoM,np.matrix(rhs))
            for i in range(0, self.ly):
                rom_params.append([])
                for j in range(0, dim):
                    rom_params[i].append(coefs[j,i])
                for j in range(1, self.lx+1):
                    rom_params[i][j]=rom_params[i][j]/radius#/radius[j-1]
                count = self.lx+1
                for ii in range(0, self.lx):
                    for j in range(ii, self.lx):
                        rom_params[i][count]=rom_params[i][count]/radius#/radius[ii]/radius[j]
                        count = count + 1

        return rom_params, y1


