# ************************************************************************
# *   Copyright (c) Stefan Troeger (stefantroeger@gmx.net) 2019          *
# *                                                                      *
# *   This library is free software; you can redistribute it and/or      *
# *   modify it under the terms of the GNU Library General Public        *
# *   License as published by the Free Software Foundation; either       *
# *   version 2 of the License, or (at your option) any later version.   *
# *                                                                      *
# *   This library  is distributed in the hope that it will be useful,   *
# *   but WITHOUT ANY WARRANTY; without even the implied warranty of     *
# *   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the      *
# *   GNU Library General Public License for more details.               *
# *                                                                      *
# *   You should have received a copy of the GNU Library General Public  *
# *   License along with this library; see the file COPYING.LIB. If not, *
# *   write to the Free Software Foundation, Inc., 59 Temple Place,      *
# *   Suite 330, Boston, MA  02111-1307, USA                             *
# ************************************************************************

import FreeCAD, FreeCADGui, asyncio, os

from Documents.Dataservice      import DataService
from Documents.Observer         import DocumentObserver, GUIDocumentObserver, ObserverManager
from Documents.OnlineDocument   import OnlineDocument

import uuid
from autobahn.wamp.types import CallResult

class DocumentHandler():
    #data structure that handles all documents for collaboration:
    # - the local ones that are unshared
    # - the ones we have been invited too but not yet joined
    # - the ones open at the node but not yet in FC
    # - the one we share
    
    def __init__(self, collab_path):               
        self.documents = [] #empty list for all our document handling status, each doc is a map: {id, status, onlinedoc, doc}
        self.updatefuncs = []
        self.connection = None
        self.collab_path = collab_path
        self.blockObserver = False
        self.uuid = uuid.uuid4()
        self.dataservice = None

        
        #add the observer 
        self.observer = DocumentObserver(self)
        self.guiObserver = GUIDocumentObserver(self)
        FreeCAD.addDocumentObserver(self.observer)
        FreeCADGui.addDocumentObserver(self.guiObserver)
    
    def setConnection(self, con):
        self.connection = con
        self.dataservice = DataService(self.uuid, con)
        #TODO check all local documents available, as this may be startet after the user opened documents in freecad     
        
        #lets initialize the async stuff!
        asyncio.ensure_future(self.asyncInit())
        
    def removeConnection(self):
        self.connection = None
        self.dataservice = None
        self.documents = {}
       

    def closeFCDocument(self, doc):
        if not self.connection:
            return 
        
        if self.blockObserver:
            return
        
        asyncio.ensure_future(self.asyncCloseDoc(doc))
        

    def openFCDocument(self, doc):
        
        if not self.connection:
            return 
        
        if self.blockObserver:
            return
        
        #If a document was opened in freecad this function makes it known to the Handler. 
        docmap = {"id": None, "status": "local", "onlinedoc": None, "fcdoc": doc}
        self.documents.append(docmap)
        print("Call update!")
        self.update()
        
    def addUpdateFunc(self, func):
        self.updatefuncs.append(func)
    
    def update(self):
        for f in self.updatefuncs:
            f()
    
    def getDocMap(self, key, val):
        #returns the docmap for the given key/value pair, e.g. "fcdoc":doc. Careful: if status is used
        #the first matching docmap is returned
        for docmap in self.documents: 
            if docmap[key] == val:
                return docmap 
        
        raise Exception('no such document found')
    
    def getOnlineDocument(self, fcdoc):
        
        #check if it is a GuiDocument 
        if hasattr(fcdoc, "ActiveView"):
            fcdoc = fcdoc.Document
        
        #get the online document for a certain freecad document, or None if not available
        for docmap in self.documents: 
            if docmap["fcdoc"] == fcdoc:
                return docmap["onlinedoc"]
        
        return None
    
    def hasOnlineViewProvider(self, vp):
        
        for docmap in self.documents: 
            odoc = docmap["onlinedoc"]
            if odoc and odoc.hasViewProvider(vp):
                return True
        
        return False
       
    async def asyncInit(self):
        #get a list of open documents of the node and add them to the list
        if not self.connection:
            return 
        
        try:
            #we register ourself for some key events
            await self.connection.session.subscribe(self.asyncOnDocumentOpened, u"ocp.documents.opened")
            await self.connection.session.subscribe(self.asyncOnDocumentClosed, u"ocp.documents.closed")
            await self.connection.session.subscribe(self.asyncOnDocumentInvited, u"ocp.documents.invited")

            res = await self.connection.session.call(u"ocp.documents.list")
            if res == None:
                doclist = []
            elif type(res) == str:
                doclist = [res]
            elif type(res) == CallResult:
                doclist = res.results
            
            for doc in doclist:
                docmap = {"id": doc, "status": "node", "onlinedoc": None, "fcdoc": None}
                self.documents.append(docmap)
            
            self.update()
            
        except Exception as e:
            print("Async init error: {0}".format(e))
    
    async def asyncStopCollaborateOnDoc(self, docmap):
        
        if not self.connection:
            return 
        
        try:
            if docmap['status'] == 'shared':
                await docmap['onlinedoc'].asyncUnload()
                await self.connection.session.call(u"ocp.documents.close", docmap['id'])
            
            docmap['status'] = "local"
            self.update()
        
        except Exception as e:
            print("Close document id error: {0}".format(e))
    
    async def asyncCloseDoc(self, doc):
        #try to disconnect before remove
        docmap = self.getDocMap('fcdoc', doc)
        self.documents.remove(docmap)
        try:
            if docmap['status'] == 'shared':
                await docmap['onlinedoc'].asyncUnload()
                await self.connection.session.call(u"ocp.documents.close", docmap['id'])
        
        except Exception as e:
            print("Close document id error: {0}".format(e))
            
        finally:            
            self.update()

       
    async def asyncCollaborateOnDoc(self, docmap):
        
        if not self.connection:
            return 
        
        try:
            obs = ObserverManager(self.guiObserver, self.observer)
            status = docmap['status']
            if status == "local":
                dmlpath = os.path.join(self.collab_path, "Dml")
                res = await self.connection.session.call(u"ocp.documents.create", dmlpath)
                docmap['id'] = res
                docmap['onlinedoc'] = OnlineDocument(res, docmap['fcdoc'], obs, self.connection, self.dataservice)
                await docmap['onlinedoc'].asyncSetup()
                
            elif status == 'node':
                self.blockObserver = True
                doc = FreeCAD.newDocument()
                self.blockObserver = False
                docmap['fcdoc'] = doc
                docmap['onlinedoc'] = OnlineDocument(docmap['id'], doc, obs, self.connection, self.dataservice)
                await docmap['onlinedoc'].asyncLoad() 
                
            elif status == 'invited':
                await self.connection.session.call(u"ocp.documents.open", docmap['id'])
                self.blockObserver = True
                doc = FreeCAD.newDocument()
                self.blockObserver = False
                docmap['fcdoc'] = doc
                docmap['onlinedoc'] = OnlineDocument(docmap['id'], doc, obs, self.connection, self.dataservice)
                await docmap['onlinedoc'].asyncUnload() 

            docmap['status'] = "shared"
            self.update()
            
        except Exception as e:
            print("collaborate error: {0}".format(e))
  
            
            
    async def asyncOnDocumentOpened(self):
        #TODO add new doc to docmap
        self.update()
        
    async def asyncOnDocumentClosed(self):
        #TODO add new doc to docmap
        self.update()
    
    async def asyncOnDocumentInvited(self):
        #TODO add new doc to docmap
        self.update()
