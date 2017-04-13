
"""
specpolwavmap

Split O and E and produce wavelength map for spectropolarimetric data

"""

import os, sys, glob, shutil, inspect
from collections import defaultdict

import numpy as np
from astropy.io import fits as pyfits
from scipy.interpolate import interp1d
from scipy.ndimage.interpolation import shift
from scipy import linalg as la

from pyraf import iraf
from iraf import pysalt

from saltobslog import obslog

import specrectify as sr
from specwavemap import wavemap
from specidentify import specidentify
from saltsafelog import logging

from specpolutils import *
from specpolsplit import specpolsplit 
from specpolwollaston import correct_wollaston, read_wollaston

datadir = os.path.dirname(__file__) + '/data/'
#np.set_printoptions(threshold=np.nan)
debug = False

def specpolwavmap(infilelist, linelistlib="", automethod='Matchlines', 
                  function='legendre', order=3, logfile='salt.log'):
    obsdate=os.path.basename(infilelist[0])[7:15]

    with logging(logfile, debug) as log:
        log.message('Pysalt Version: '+pysalt.verno, with_header=False)
      
        # group the files together
        config_dict = list_configurations(infilelist, log)

        for config in config_dict:
            if len(config_dict[config]['arc']) == 0:
                log.message('No Arc for this configuration:', with_header=False)
                continue
        #set up some information needed later
            iarc = config_dict[config]['arc'][0]
            hduarc = pyfits.open(iarc)
            image_no = image_number(iarc)
            rows, cols = hduarc[1].data.shape
            grating = hduarc[0].header['GRATING'].strip()
            grang = hduarc[0].header['GR-ANGLE']
            artic = hduarc[0].header['CAMANG']
            filter = hduarc[0].header['FILTER'].strip()

            cbin, rbin = [int(x) for x in hduarc[0].header['CCDSUM'].split(" ")]

            # need this for the distortion correction 
            rpix_oc = read_wollaston(hduarc, wollaston_file=datadir+"wollaston.txt")

            #split the arc into the two beams
            hduarc, splitrow = specpolsplit(hduarc, splitrow=None, wollaston_file=datadir+"wollaston.txt")

            #set up the lamp to be used
            lamp=hduarc[0].header['LAMPID'].strip().replace(' ', '')
            if lamp == 'NONE': lamp='CuAr'
            
            # set up the linelist to be used
            if len(linelistlib):                # if linelistlib specified, use salt-supplied
                with open(linelistlib) as fd:
                    linelistdict = dict(line.strip().split(None, 1) for line in fd)
                lampfile=iraf.osfn("pysalt$data/linelists/"+linelistdict[lamp]) 
            else:                               # else, use line lists in polarimetry area for 300l
                if grating=="PG0300": 
                    linelistlib=datadir+"linelistlib_300.txt"
                    lib_lf = list(np.loadtxt(linelistlib,dtype=str,usecols=(0,1,2)))    # lamp,filter,file
                    linelistdict = defaultdict(dict)
                    for ll in range(len(lib_lf)):
                        linelistdict[lib_lf[ll][0]][int(lib_lf[ll][1])] = lib_lf[ll][2] 
                    filter_l = np.sort(np.array(linelistdict[lamp].keys()))
                    usefilter = filter_l[np.where(int(filter[-5:-1]) < filter_l)[0][0]]
                    lampfile = datadir+linelistdict[lamp][usefilter]
                else:
                    linelistlib=datadir+"linelistlib.txt"
                    with open(linelistlib) as fd:
                        linelistdict = dict(line.strip().split(None, 1) for line in fd)   
                    lampfile=iraf.osfn("pysalt$data/linelists/"+linelistdict[lamp])  

            # some housekeeping for bad keywords
            if hduarc[0].header['MASKTYP'].strip() == 'MOS':   # for now, MOS treated as single, short 1 arcsec longslit
                hduarc[0].header['MASKTYP'] = 'LONGSLIT'
                hduarc[0].header['MASKID'] = 'P001000P99'
            del hduarc['VAR']
            del hduarc['BPM']
    
            # log the information about the arc
            log.message('\nARC: image '+str(image_no)+' GRATING '+grating\
                        +' GRANG '+("%8.3f" % grang)+' ARTIC '+("%8.3f" % artic)+' LAMP '+lamp, with_header=False)
            log.message('  Split Row: '+("%4i " % splitrow), with_header=False)

            # set up the correction for the beam splitter
            drow_oc = (rpix_oc-rpix_oc[:,cols/2][:,None])/rbin

            wavmap_orc = pol_wave_map(hduarc, image_no, drow_oc, rows, cols,
                                      lampfile=lampfile, function=function, order=order,
                                      automethod=automethod, log=log, logfile=logfile)


            # for images using this arc,save split data along third fits axis, 
            # add wavmap extension, save as 'w' file
            hduwav = pyfits.ImageHDU(data=wavmap_orc.astype('float32'), header=hduarc['SCI'].header, name='WAV')                 
            for image in config_dict[config]['object']:
                hdu = pyfits.open(image)
                hdu, splitrow = specpolsplit(hdu, splitrow=splitrow)
                hdu['BPM'].data[wavmap_orc==0.] = 1 
                hdu.append(hduwav)
                for f in ('SCI','VAR','BPM','WAV'): hdu[f].header['CTYPE3'] = 'O,E'
                hdu.writeto('w'+image,clobber='True')
                log.message('Output file '+'w'+image, with_header=False)

    return

def pol_wave_map(hduarc, image_no, drow_oc, rows, cols, lampfile, 
                 function='legendre', order=3, automethod="Matchlines",
                 log=None, logfile=None):
    """ Create a wave_map for an arc image

    For O,E arc straighten spectrum, find fov, identify for each, form (unstraightened) wavelength map   
    this corrects for the aberration introduced by the beam splitter
    this will be removed back out when creating the wave map
 
    Parameters 
    ----------
    hduarc: fits.HDUList
       Polarimetric arc data. This data should be split into O+E beams

    image_no: int
       File number of observations

    rows: int
       Nubmer of rows in original data

    cols: int
       Nubmer of columns in original data

    lampfile: str
       File name containing line list 

    function: str
       Function used for wavelength fitting

    order: int
       Order of fitting function

    automethod: str
       Method for automated line identification

    log: log
       Log for output

    Returns
    -------
    wavmap: numpy.ndarray
       Wave map of wavelengths correspond to pixels

    """

    arc_orc =  hduarc[1].data
    cbin, rbin = [int(x) for x in hduarc[0].header['CCDSUM'].split(" ")]
    axisrow_o = np.array([rows/4.0, rows/4.0]).astype(int)
    grating = hduarc[0].header['GRATING'].strip()
    grang = hduarc[0].header['GR-ANGLE']
    artic = hduarc[0].header['CAMANG']
    trkrho = hduarc[0].header['TRKRHO']  
    date =  hduarc[0].header['DATE-OBS'].replace('-','')  

    #set up some output arrays
    wavmap_orc = np.zeros((2,rows/2,cols))
    edgerow_od = np.zeros((2,2))
    cofrows_o = np.zeros(2)
    legy_od = np.zeros((2,2))

    lam_X = rssmodelwave(grating,grang,artic,trkrho,cbin,cols,date)
    np.savetxt("lam_X_"+str(image_no)+".txt",lam_X,fmt="%8.3f")
    C_f = np.polynomial.legendre.legfit(np.arange(cols),lam_X,3)[::-1]
    dbhdr = open(datadir+"arcdb_guesshdr.txt").readlines()


    for o in (0,1):
        #correct the shape of the arc for the distortions
        arc_yc = correct_wollaston(arc_orc[o], -drow_oc[o])

        # this is used to remove rows outside the slit
        maxoverlaprows = 34/rbin                        # beam overlap for 4' longslit in NIR
        arc_y = arc_yc.sum(axis=1)
        arc_y[[0,-1]] = 0.

        edgerow_od[o,0] = axisrow_o[o] - np.argmax(arc_y[axisrow_o[o]::-1] <  0.5*arc_y[axisrow_o[o]])
        edgerow_od[o,1] = axisrow_o[o] + np.argmax(arc_y[axisrow_o[o]:] <  0.5*arc_y[axisrow_o[o]])
        axisrow_o[o] = edgerow_od[o].mean()
        if np.abs(edgerow_od[o] - np.array([0,rows/2-1])).min() < maxoverlaprows:
            edgerow_od[o] += maxoverlaprows*np.array([+1,-1])

        #write out temporary image to run specidentify
        hduarc['SCI'].data = arc_yc
        arcimage = "arc_"+str(image_no)+"_"+str(o)+".fits"
        dbfilename = "arcdb_"+str(image_no)+"_"+str(o)+".txt"
        otherdbfilename = "arcdb_"+str(image_no)+"_"+str(int(not(o==1)))+".txt"
        guessfilename = ""
        ystart = axisrow_o[o]

        if (not os.path.exists(dbfilename)):
            if (os.path.exists(otherdbfilename)):
                row_Y = np.loadtxt(otherdbfilename,dtype=float,usecols=(0,),ndmin=1)
                closestrow = row_Y[np.argmin(np.abs(row_Y - ystart))]
                guessfilename="wavguess_"+str(image_no)+"_"+str(o)+".txt"  
                guessfile=open(guessfilename, 'w')            
                for line in open(otherdbfilename):
                    if (len(line.split())==0): continue             # ignore naughty dbfile extra lines
                    if (line[0] == "#"):
                        guessfile.write(line)
                    elif (float(line.split()[0]) == closestrow):
                        guessfile.write(line)
                guessfile.close()
                guesstype = 'file'
#                guesstype = 'rss'
            else:
#               open(guessfile,'w').writelines(dbhdr+[("%8.2f "+4*"%12.5e ") % ((ystart,)+tuple(C_f[::-1]))])
                guesstype = 'rss'            
            hduarc.writeto(arcimage,clobber=True)
                 
            specidentify(arcimage, lampfile, dbfilename, guesstype=guesstype,
                guessfile=guessfilename, automethod=automethod,  function=function,  order=order,
                rstep=20, rstart=ystart, mdiff=20, thresh=3, niter=5, smooth=3,
                inter=True, clobber=True, logfile=logfile, verbose=True)
            if (not debug): os.remove(arcimage)
                
        wavmap_yc, cofrows_o[o], legy_od[o], edgerow_od[o] = \
                wave_map(dbfilename, edgerow_od[o], rows, cols, ystart, order, log=log)
        #TODO: Once rest is working, try to switch to pysalt wavemap
        #soldict = sr.entersolution(dbfilename)
        #wavmap_yc = wavemap(hduarc, soldict, caltype='line', function=function, 
        #          order=order,blank=0, nearest=True, array_only=True,
        #          clobber=True, log=log, verbose=True)
                  
        # put curvature back in, zero out areas beyond slit and wavelength range (will be flagged in bpm)
        if debug: np.savetxt("drow_wmap_oc.txt",drow_oc.T,fmt="%8.3f %8.3f")
        wavmap_orc[o] = correct_wollaston(wavmap_yc,drow_oc[o])

        y, x = np.indices(wavmap_orc[o].shape)

# fixing problem in 0312 sc wavmap
        notwav_c = np.isnan(drow_oc[o])
        drow_oc[o,notwav_c] = 0.
        mask = (y < edgerow_od[o,0] + drow_oc[o]) | (y > edgerow_od[o,1] + drow_oc[o])

        wavmap_orc[o,mask] = 0.
        wavmap_orc[o][:,notwav_c] = 0.
#

    if log is not None:
        log.message('\n  Wavl coeff rows:  O    %4i     E    %4i' % tuple(cofrows_o), with_header=False)
        log.message('  Bottom, top row:  O %4i %4i   E %4i %4i' \
            % tuple(legy_od.flatten()), with_header=False)
        log.message('\n  Slit axis row:    O    %4i     E    %4i' % tuple(axisrow_o), with_header=False)
        log.message('  Bottom, top row:  O %4i %4i   E %4i %4i \n' \
            % tuple(edgerow_od.flatten()), with_header=False)

    return wavmap_orc

 
def wave_map(dbfilename, edgerow_d, rows, cols, ystart, order=3, log=None):
    """Read in the solution file and create a wave map from the solution

    Parameters
    ---------- 
    dbfilename: str
        File with wavelength solutions

    edgerow_od: numpy.ndarray
        Numpy array with lower and upper limits

    rows: int
        Number of rows in original data

    cols: int
        Number of columns in original data

    order: int
        Order of function to be fit

    Returns
    -------
    wavmap_yc: numpy.ndarray
        Map with wavelength for each pixel position

    """
    # process dbfile legendre coefs within FOV into wavmap (_Y = line in dbfile)
    legy_Y = np.loadtxt(dbfilename,dtype=float,usecols=(0,),ndmin=1)
    dblegcof_lY = np.loadtxt(dbfilename,unpack=True,dtype=float,usecols=range(1,order+2),ndmin=2)
    hasdomain = False
    for line in open(dbfilename):
        if line[1:7] == "domain":
            domain_c = np.array(line[8:].split(',')).astype(float)
            hasdomain = True
            break

    if hasdomain:
        xcenter = domain_c.mean()
        legcof_lY = dblegcof_lY
        xfit_c = 2.*(np.arange(cols) - xcenter)/(domain_c[1]-domain_c[0])   
    else:
      # convert to centered legendre coefficients to remove crosscoupling
        xcenter = cols/2.
        legcof_lY = np.zeros_like(dblegcof_lY)
        legcof_lY[2] = dblegcof_lY[2] + 5.*dblegcof_lY[3]*xcenter
        legcof_lY[3] = dblegcof_lY[3]
        legcof_lY[0] = 0.5*legcof_lY[2] + (dblegcof_lY[0]-dblegcof_lY[2]) + \
            (dblegcof_lY[1]-1.5*dblegcof_lY[3])*xcenter + 1.5*dblegcof_lY[2]*xcenter**2 + \
            2.5*dblegcof_lY[3]*xcenter**3
        legcof_lY[1] = 1.5*legcof_lY[3] + (dblegcof_lY[1]-1.5*dblegcof_lY[3]) + \
            3.*dblegcof_lY[2]*xcenter + 7.5*dblegcof_lY[3]*xcenter**2
        xfit_c = np.arange(-cols/2,cols/2)

    # remove rows outside slit
    argYbad = np.where((legy_Y<edgerow_d[0]) | (legy_Y>edgerow_d[1]))[0]
    legy_Y = np.delete(legy_Y, argYbad,axis=0)
    legcof_lY = np.delete(legcof_lY, argYbad,axis=1)
    cofrows = legy_Y.shape[0]
    if cofrows > 3:
    # remove outlier fits
        mediancof_l = np.median(legcof_lY,axis=1)
        rms_l = np.sqrt(np.median((legcof_lY - mediancof_l[:,None])**2,axis=1))
        sigma_lY = np.abs((legcof_lY - mediancof_l[:,None]))/rms_l[:,None]
        argYbad = np.where((sigma_lY>4).any(axis=0))[0]
        legy_Y = np.delete(legy_Y, argYbad,axis=0)
        legcof_lY = np.delete(legcof_lY, argYbad,axis=1)
        cofrows = legy_Y.shape[0]

    if cofrows < 5:
    # assume this is short MOS slit: use ystart solution for all rows, undo the slit edge settings
        log.message('FEW DATABASE ROWS, ASSUME MOS, USE START' , with_header=False)                    
        legcof_l = legcof_lY[:,legy_Y.astype(int)==ystart].ravel()
        wavmap_yc = np.tile(np.polynomial.legendre.legval(xfit_c,legcof_l)[:,None],rows/2).T
        edgerow_d = 0,rows/2
        cofrows = 1
        legy_d = ystart,ystart
    else:
    # smooth wavmap along rows by fitting L_0 to quadratic, others to linear fn of row
        ycenter = rows/4.
        Y_y = np.arange(-ycenter,ycenter)
        aa = np.vstack(((legy_Y-ycenter)**2,(legy_Y-ycenter),np.ones(cofrows))).T
        polycofs = la.lstsq(aa,legcof_lY[0])[0]
        legcof_ly = np.zeros((order+1,rows/2))
        legcof_ly[0] = np.polyval(polycofs,Y_y)
        for l in range(1,order+1):
            polycofs = la.lstsq(aa[:,1:],legcof_lY[l])[0]
            legcof_ly[l] = np.polyval(polycofs,Y_y)
        wavmap_yc = np.zeros((rows/2,cols))
        for y in range(rows/2):
            wavmap_yc[y] = np.polynomial.legendre.legval(xfit_c,legcof_ly[:,y])
        legy_d = legy_Y.min(),legy_Y.max()

    return wavmap_yc, cofrows, legy_d, edgerow_d
