# zwiki page hierarchy functionality
# based on original code by Ken Manheimer
#
# The OutlineSupport mixin manages the wiki's page hierarchy, making use
# of the more generic Outline class.  It is broken into several smaller
# mixins to help keep things organized.
#
# glossary:
#  primary parent: our designated parent page, or the first if more than one
#  siblings: pages which share a parent with us
#  context: our parents and their parents up to the top, and maybe our siblings
#  children: pages for which we are a parent
#  offspring: all our children and their children to the bottom
#  subtopics: end-user name for offspring or children
#  nesting: nested list structure representing a piece of hierarchy
#  outline: an Outline object, encapsulatng a nesting and other info
#  wiki outline, page hierarchy: the hierarchy of pages within a wiki
#
# todo:
# evaluate experimental separate outline structure
# -cache wiki nesting structure on folder
# -encapsulate it as an Outline object
# -make code work with an Outline object
# -move generic operations there
# -remove getWikiParentInfo
# -add mutators to help with syncing
# -add
# -delete
# -replace
# -reparent
# -add tests and keep it synced in parallel with parents attributes
# -keep it synced during rename
# -keep it synced during create
# -keep it synced during delete
# -keep it synced during reparent
# -keep it synced during ZMI operations
# -check mutators for persistence compatibility ?
# -refactor OutlineSupport
# try dropping parents property/index/metadata
# upgrade process
# code cleanup/renaming
# simplify Outline implementation
# remove Outline/nesting split ?
# refactor/simplify OutlineRenderingMixin

from __future__ import nested_scopes
import string, re
from string import join
from types import *
from urllib import quote, unquote

from AccessControl import ClassSecurityInfo
import Acquisition
from App.Common import absattr
from Globals import InitializeClass
import Persistence
from OFS.SimpleItem import SimpleItem

import Permissions
from Utils import flatten, BLATHER
from Defaults import PAGE_METATYPE, SHOW_CURRENT_PAGE_IN_CONTENTS
from Regexps import bracketedexpr
import Outline

from I18nSupport import _

def deepappend(nesting, page):
    """
    Append a page to the very bottom of a nesting.
    """
    if type(nesting[-1]) is type([]):
        nesting[-1] = deepappend(nesting[-1], page)
    else:
        if len(nesting) is 1: nesting.append(page)
        else: nesting[-1] = [nesting[-1],page]
    return nesting

class PersistentOutline(Outline.Outline, SimpleItem):
    """
    I am a persistent version of Outline.

    XXX do my mutators need to do more of this sort of thing ?:
    # do an attr assignment in case we ever cache this as a persistent object
    if childmap.has_key(parent): pchildren = childmap[parent]
    else: pchildren = []
    if name not in pchildren: pchildren.append(name)
    childmap[parent] = pchildren
    """
    pass

class ParentsPropertyMixin:
    """
    I provide the parents property, the old way to store page hierarchy.

    For now we keep this property up to date, in sync with the wiki
    outline, but we'll try removing it completely at some point.

    Although it's a simple and relatively robust design, it doesn't scale
    so well: generating the hierarchy structure from the parents
    properties each time is expensive, and although modifying
    decentralized parents properties seems better than a shared outline
    object from the perspective of avoiding conflict errors, you still
    need to update the central catalog so there may be no great advantage.
    """
    security = ClassSecurityInfo()

    parents = []
    _properties=(
        {'id':'parents', 'type': 'lines', 'mode': 'w'},
        )

    def ensureParentsPropertyIsList(self):
        """
        Ensure our parents property is a list, returning true if changed.

        Zope lines properties switched from tuple to list a while back,
        and lingering tuples cause ongoing breakage.  Called by upgrade,
        ensureValidParents and accessors for maximum robustness.
        """
        if type(self.parents) != ListType:
            BLATHER("converting %s's parents property to a list" % self.pageName())
            self.setParents(self.parents)
            return True
        else:
            return False

    # I'd call this parents, but too many upgrade hassles ?
    def getParents(self):
        """
        Robust accessor returning a copy of our parents list.
        """
        return list(self.parents)

    def setParents(self,parents):
        parents = list(parents)
        parents.sort()
        self.parents = parents

    def addParent(self,parent):
        if parent:
            # we sometimes start page names with space as a subtopic
            # ordering hack.. 
            #parent = string.strip(parent)
            if parent and not parent in self.parents:
                self.ensureParentsPropertyIsList()
                self.parents.append(parent)

    def removeParent(self,parent):
        self.ensureParentsPropertyIsList()
        try: self.parents.remove(parent)
        except ValueError:
            BLATHER("failed to remove %s from %s's parents (%s)" \
                 % (parent,self.getId(),self.parents))

    def ensureValidParents(self):
        """
        Ensure that this page's parents are all valid, and reindex if needed.
        """
        parents = self.getParents()
        # convert to exact page names, filtering out any which don't exist
        cleanedupparents = map(lambda x:absattr(x.Title),
                               filter(lambda x:x,
                                      map(lambda x:self.pageWithName(x),
                                          parents)))
        # make sure we're not parented under ourself
        if self.pageName() in cleanedupparents:
            cleanedupparents.remove(self.pageName())
        # sort
        cleanedupparents.sort()
        # if changed, save and reindex
        if cleanedupparents != parents:
            BLATHER("adjusting %s's parents from %s to %s" % 
                 (self.pageName(), parents, cleanedupparents))
            self.setParents(cleanedupparents)
            self.index_object() #XXX only need to update parents index & metadata

InitializeClass(ParentsPropertyMixin) 

class OutlineManagerMixin:
    """
    I manage and query a cached outline object for the wiki.

    When first accessed I generate it based on the parents properties and
    cache it as a folder attribute.  This is the new way to represent page
    hierarchy.

    old:
    KM: We could make this a persistent object and minimize recomputes.
    Put it in a standard place in the wiki's folder, or have the pages in
    a folder share an instance, but use a single persistent one which need
    not recompute all the relationship maps every time - just needs to
    compare all pages parents settings with the last noticed parents
    settings, and adjust the children, roots, and parents maps just for
    those that changed.
    """
    security = ClassSecurityInfo()

    # mutators

    security.declareProtected(Permissions.View, 'updateWikiOutline')
    def updateWikiOutline(self):
        """
        Regenerate the wiki folder's cached outline object.

        The wiki's outline object (a PersistentOutline) is a
        representation of the page hierarchy, containing the same
        information as in the pages' parents properties but in a form
        easier to query. This method either generates a new one from the
        parents properties, or updates an old one trying to preserve the
        order of subtopics. Complications.

        This checks and corrects any invalid parents information.  It also
        loads all page objects from the ZODB, which is probably ok as this
        is not done frequently.
        """
        BLATHER('regenerating outline data for wiki',self.folder().getId())
        oldchildmap = {}
        # backwards compatibility
        # a pre-0.39 outline is just an attribute, delete (but save childmap)
        if (hasattr(self.folder().aq_base,'outline') and
            not 'outline' in self.folder().objectIds()):
            oldchildmap = self.folder().outline.childmap()
            del self.folder().outline
        # if there's no outline object, make one
        if not hasattr(self.folder().aq_base,'outline'):
            self.folder()._setObject('outline', PersistentOutline())
            self.folder().outline.setChildmap(oldchildmap)
        # regenerate the parentmap
        parentmap = {}
        for p in self.pageObjects():
            p.ensureValidParents() # poor caching
            parentmap[p.pageName()] = p.getParents()
        self.folder().outline.setParentmap(parentmap)
        # update the childmap (without losing subtopics order) and nesting
        self.folder().outline.update()

    # easier alias ?
    updatecontents = updateWikiOutline
        
    security.declareProtected(Permissions.Reparent, 'reparent')
    def reparent(self, parents=[], REQUEST=None, pagename=None):
        """
        Move this page under the named parent pages in the wiki outline.

        parent page names may be passed in several ways:
        - in the parents argument (a list or string of page names)
        - in the pagename argument (a single name)
        (this last is to support the page management form).

        Page names may be ids, or fuzzy, even partial. Any which do not
        resolve to an existing page or are duplicates will be ignored.
        """
        # clean the arguments carefully to avoid parenting anomalies
        # page mgmt form must use pagename field:
        if pagename:
            parents = [pagename] 
        # or parents might be a string
        elif type(parents) != ListType:
            parents = [parents] 
        # empty strings are common, remove before calling pageWithFuzzyName
        parents = filter(lambda x:x, parents)
        # look up the page (brain) corresponding to each (fuzzy) parent name
        parents = map(lambda x:self.pageWithFuzzyName(x,allow_partial=1),
                      parents)
        # strip out Nones (pages not found)
        parents = filter(lambda x:x, parents)
        # convert back to proper page names
        parents = map(lambda x:absattr(x.Title), parents)
        # remove any duplicates
        uniqueparents = []
        for p in parents:
            if not p in uniqueparents: uniqueparents.append(p)
            
        # finally - update our parents property, the outline cache, and catalog
        self.setParents(uniqueparents) 
        self.wikiOutline().reparent(self.pageName(),uniqueparents)
        self.index_object()

        if REQUEST is not None: REQUEST.RESPONSE.redirect(REQUEST['URL1'])

    security.declareProtected(Permissions.Reparent, 'reorder')
    def reorder(self, child, REQUEST=None):
        """
        Move child one place to the left among this page's children.

        Ordering is only stored in the wiki outline cache, and will be
        lost if the outline is completely regenerated.

        When invoked from the web, redirects to /backlinks.
        """
        if not child in self.childrenAsList(): return
        self.wikiOutline().reorder(self.pageName(),child)
        if REQUEST is not None: REQUEST.RESPONSE.redirect(REQUEST['URL1']+'/backlinks')

    # queries

    def wikiOutline(self):
        """
        Get the outline object representing the wiki's page hierarchy.

        We'll generate it if needed.
        """
        if (not hasattr(self.folder().aq_base,'outline')
            or not self.folder().outline):
            self.updateWikiOutline()
        return self.folder().outline

    def primaryParentName(self):
        """
        Get the name of this page's primary (alphabetically first) parent.
        """
        return self.wikiOutline().firstParent(self.pageName())

    def primaryParent(self):
        """
        Return this page's primary parent page.
        """
        p = self.primaryParentName()
        if p: return self.pageWithName(p)
        else: return None

    def primaryParentUrl(self):
        """
        Get the URL of this page's primary parent.
        """
        p = self.primaryParent()
        if p: return p.pageUrl()
        else: return None

    def upUrl(self):
        """
        Get the URL of whatever is "above" this page.
        """
        return self.primaryParentUrl() or self.defaultPageUrl()

    security.declareProtected(Permissions.View, 'firstPage')
    def firstPage(self):
        """
        Get the name of the first page in the hierarchy.
        """
        return self.wikiOutline().first()

    security.declareProtected(Permissions.View, 'firstPageUrl')
    def firstPageUrl(self):
        """
        Get the URL of the first page in the hierarchy.
        """
        p = self.pageWithName(self.firstPage())
        if p: return p.pageUrl()
        else: return None

    security.declareProtected(Permissions.View, 'lastPage')
    def lastPage(self):
        """
        Get the name of the last page in the hierarchy.
        """
        return self.wikiOutline().last()

    security.declareProtected(Permissions.View, 'lastPageUrl')
    def lastPageUrl(self):
        """
        Get the URL of the last page in the hierarchy.
        """
        p = self.pageWithName(self.lastPage())
        if p: return p.pageUrl()
        else: return None

    security.declareProtected(Permissions.View, 'nextPage')
    def nextPage(self):
        """
        Get the name of the next page in the hierarchy.

        XXX nextPageName ?
        """
        return self.wikiOutline().next(self.pageName())

    security.declareProtected(Permissions.View, 'nextPageUrl')
    def nextPageUrl(self):
        """
        Get the URL of the next page in the hierarchy.
        """
        p = self.pageWithName(self.nextPage())
        if p: return p.pageUrl()
        else: return None

    security.declareProtected(Permissions.View, 'previousPage')
    def previousPage(self):
        """
        Get the name of the previous page in the hierarchy.
        """
        return self.wikiOutline().previous(self.pageName())

    security.declareProtected(Permissions.View, 'previousPageUrl')
    def previousPageUrl(self):
        """
        Get the URL of the previous page in the hierarchy.
        """
        p = self.pageWithName(self.previousPage())
        if p: return p.pageUrl()
        else: return None

    security.declareProtected(Permissions.View, 'ancestorsAsList')
    def ancestorsAsList(self, REQUEST=None):
        """
        Return the names of all my ancestor pages as a flat list, eldest first.

        If there are multiple lines of ancestry, return only the first.
        """
        try: return flatten(self.ancestorsNesting())[:-1]
        except: return [] # XXX temp, get rid of

    security.declareProtected(Permissions.View, 'siblingsAsList')
    def siblingsAsList(self):
        """
        Return the names of other pages sharing my first parent.

        Siblings by my other parents are ignored.
        """
        return self.wikiOutline().siblings(self.pageName())

    security.declareProtected(Permissions.View, 'childrenAsList')
    def childrenAsList(self):
        """
        Return the list of names of my immediate children, if any.
        """
        return self.wikiOutline().children(self.pageName())

    security.declareProtected(Permissions.View, 'childrenIdsAsList')
    def childrenIdsAsList(self, REQUEST=None):
        """
        Return all my children's page ids as a flat list.
        """
        return map(lambda x:absattr(self.pageWithNameOrId(x).id),
                   self.childrenAsList())

    security.declareProtected(Permissions.View, 'offspringAsList')
    def offspringAsList(self, REQUEST=None):
        """
        Return my offsprings' page names as a flat list, excluding my name.
        """
        list = flatten(self.offspringNesting())
        list.remove(self.pageName())
        return list

    security.declareProtected(Permissions.View, 'offspringIdsAsList')
    def offspringIdsAsList(self, REQUEST=None):
        """
        Return my offsprings' page ids as a flat list.
        """
        return map(lambda x:absattr(self.pageWithNameOrId(x).id),
                   self.offspringAsList())

    # queries returning nestings (lists-of-lists) - a legacy
    # representation of all or part of the wiki outline, which can be
    # processed by the render methods.

    def ancestorsNesting(self):
        """
        Return a nesting representing this page's ancestors.
        """
        return self.wikiOutline().ancestors(self.pageName())

    def ancestorsAndSiblingsNesting(self):
        """
        Return a nesting representing this page's ancestors and siblings.
        """
        return self.wikiOutline().ancestorsAndSiblings(self.pageName())

    def ancestorsAndChildrenNesting(self):
        """
        Return a nesting representing this page's ancestors and children.
        """
        return self.wikiOutline().ancestorsAndChildren(self.pageName())

    def childrenNesting(self):
        """
        Return a nesting representing this page's ancestors and children.
        """
        return self.wikiOutline().children(self.pageName())

    def offspringNesting(self):
        """
        Return a nesting representing this page's descendants.
        """
        return self.wikiOutline().offspring([self.pageName()])

InitializeClass(OutlineManagerMixin)

class SubtopicsPropertyMixin:
    """
    I determine when to display subtopics on a page.
    """
    security = ClassSecurityInfo()

    def subtopicsEnabled(self,**kw):
        """
        Decide in a complicated way if this page should display it's subtopics.

        First, if the folder has a show_subtopics property (can acquire)
        and it's false, we will never show subtopics.  Otherwise look for
        a show_subtopics property
        - in REQUEST
        - on the current page
        - on our primary ancestor pages, all the way to the top,
        and return the first one we find. Otherwise return true.
        """
        prop = 'show_subtopics'
        if getattr(self.folder(),prop,1):
            if kw.has_key(prop):
                return kw[prop] and 1
            elif hasattr(self,'REQUEST') and hasattr(self.REQUEST,prop):
                return getattr(self.REQUEST,prop) and 1
            elif hasattr(self.aq_base,prop):
                return getattr(self,prop) and 1
            elif self.primaryParent():
                # poor caching
                try: return self.primaryParent().subtopicsEnabled() 
                except:
                    # experimental: support all-brains
                    try: return self.primaryParent().getObject().subtopicsEnabled() 
                    except: # XXX still run into errors here, investigate
                        BLATHER('DEBUG: error in subtopicsEnabled for %s, primaryParent is: %s'\
                             % (self.id(),`self.primaryParent()`))
                        return not (getattr(getattr(self,'REQUEST',None),
                                            'zwiki_displaymode',
                                            None) == 'minimal')
            else:
                #return not (getattr(getattr(self,'REQUEST',None),
                #                    'zwiki_displaymode',
                #                    None) == 'minimal')
                return 1
        else:
            return 0

    def subtopicsPropertyStatus(self):
        """
        Get the status of the show_subtopics property on this page.

        no property:    -1 ("default")
        true property:   1 ("always")
        false property:  0 ("never")
        """
        if not hasattr(self.aq_base,'show_subtopics'): return -1
        else: return self.show_subtopics and 1

    def setSubtopicsPropertyStatus(self,status,REQUEST=None):
        """
        Set, clear or remove this page's show_subtopics property.

        Same values as getSubtopicsStatus.
        """
        props = map(lambda x:x['id'], self._properties)
        if status == -1:
            if 'show_subtopics' in props:
                self.manage_delProperties(ids=['show_subtopics'],
                                          REQUEST=REQUEST)
        elif status:
            if not 'show_subtopics' in props:
                self.manage_addProperty('show_subtopics',1,'boolean',
                                        REQUEST=REQUEST)
            else:
                self.manage_changeProperties(show_subtopics=1,
                                             REQUEST=REQUEST)
        else:
            if not 'show_subtopics' in props:
                self.manage_addProperty('show_subtopics',0,'boolean',
                                        REQUEST=REQUEST)
            else:
                self.manage_changeProperties(show_subtopics=0,
                                             REQUEST=REQUEST)

InitializeClass(SubtopicsPropertyMixin)

class OutlineRenderingMixin:
    """
    I present various parts of the wiki outline as HTML.

    Some code cleanup here would be nice.
    """
    security = ClassSecurityInfo()

    security.declareProtected(Permissions.View, 'contents')
    def contents(self, REQUEST=None, here=None):
        """
        Show the entire page hierarchy, using the contentspage template.

        Includes all the branches in the wiki - from the possibly multiple
        roots - and all singletons, ie those without parents or children.
        The page named by here, or the current page, will be highlighted
        with "you are here".
        """
        nesting = self.wikiOutline().nesting()
        singletons = []
        combos = []
        baseurl = self.wiki_url()
        for i in nesting:
            if type(i) == StringType:
                #try:
                #    # XXX poor caching ?
                #    linktitle = self.folder()[i].linkTitle()
                #except:
                linktitle = ''
                singletons.append(\
                    '<a href="%s/%s" name="%s" title="%s">%s</a>'\
                    % (baseurl, self.canonicalIdFrom(i), quote(i),
                       linktitle,
                       self.formatWikiname(i)))
            else:
                combos.append(i)
        here = (here and unquote(here)) or self.pageName()
        return self.contentspage(
            self.renderNesting(combos, here),
            #self.renderNestingX(combos, here),  # dan's skinnable version
            singletons,
            REQUEST=REQUEST)

    security.declareProtected(Permissions.View, 'context')
    def context(self, REQUEST=None, with_siblings=0, enlarge_current=0):
        """
        Return HTML showing this page's ancestors and siblings.

        XXX how can we use a page template for this ? macro ?
        """
        # get the nesting structure
        here = self.pageName()
        if with_siblings:
            nesting = self.ancestorsAndSiblingsNesting()
        else:
            # why does the above require a nesting and not this one ?
            # nesting = self.get_ancestors()
            #nesting = WikiNesting(self.folder()).get_ancestors(here,self)
            nesting = self.ancestorsNesting()
            # XXX looks like cruft
            if (len(nesting) == 0  or
                (len(nesting) == 1 and len(nesting[0]) == 1)) and not enlarge_current:
                return "&nbsp;"

        # format and link it
        # backwards compatibility: in case of an old editform template
        # which shows context, include the new page name at the bottom (unlinked)
        if REQUEST and REQUEST.has_key('page') and REQUEST['page'] != here:
            here = REQUEST['page']
            nesting = deepappend(nesting, here)
            suppress_hyperlink=1
        else:
            suppress_hyperlink=0
        hierarchy = self.renderNesting(
            nesting, here,
            enlarge_current=enlarge_current,
            suppress_hyperlink=suppress_hyperlink)
        # special case: if parent seems to be missing, reset XXX
        if hierarchy == '<ul>\n</ul>':
            self.setParents([])
            self.index_object()
            hierarchy = self.renderNesting(
                nesting, here, 
                enlarge_current=enlarge_current,
                suppress_hyperlink=suppress_hyperlink)
        # if a SiteMap page exists, point the contents link there
        contentsurl = self.contentsUrl()
        contentslink = \
          '<a href="%s" title="show wiki contents">%s contents</a>' \
          % (contentsurl, self.folder().title)
        #return '<small><ul>%s\n%s\n</ul></small>' % (contentslink,hierarchy)
        #XXX try no contents link in context
        return '<small>%s\n</small>' % (hierarchy)

    security.declareProtected(Permissions.View, 'contextX')
    def contextX(self, REQUEST=None, with_siblings=0):
        """
        Return this page's context as nesting structure in a dictionary.

        Like context, but allows a skin template to control the rendering,
        """
        # get the nesting structure
        here = self.pageName()
        if with_siblings:
            #nesting = WikiNesting(self.folder()).get_up_and_back(here)
            nesting = self.ancestorsAndChildrenNesting()
        else:
            # why does the above require a nesting and not this one ?
            # nesting = self.get_ancestors()
            #nesting = WikiNesting(self.folder()).get_ancestors(here,self)
            nesting = self.ancestorsNesting()
            # XXX looks like cruft
            #if (len(nesting) == 0  or
            #    (len(nesting) == 1 and len(nesting[0]) == 1)):
            #    return {'contentsUrl':self.contentsUrl(),
            #        'hierarchy':{}}

        # format and link it
        # backwards compatibility: in case of an old editform template
        # which shows context, include the new page name at the bottom (unlinked)
        if REQUEST.has_key('page') and REQUEST['page'] is not here:
            here = REQUEST['page']
            nesting = deepappend(nesting, here)
            suppress_hyperlink=1
        else:
            suppress_hyperlink=0

        hierarchy = self.renderNestingX( nesting, here )
            
        # special case: if parent seems to be missing, reset XXX
        if len(hierarchy) == 2 :
            self.setParents([])
            self.index_object()
            hierarchy = self.renderNestingX(
                nesting, here)
                
        # if a SiteMap page exists, point the contents link there
        return {'contentsUrl':self.contentsUrl(), 'hierarchy':hierarchy}

    security.declareProtected(Permissions.View, 'children')
    def children(self):
        """
        Return HTML showing my immediate children, if any.
        """
        children = self.childrenAsList()
        if children:
            return self.renderNesting(children)
        else:
            return ''

    security.declareProtected(Permissions.View, 'offspring')
    def offspring(self, REQUEST=None, info=None, exclude_self=0):
        """
        Return HTML displaying all my offspring.
        """
        here = self.pageName()
        return self.renderNesting(
            self.offspringNesting(),
            here,
            suppress_current=exclude_self)

    security.declareProtected(Permissions.View, 'subtopics')
    def subtopics(self, REQUEST=None):
        """
        Render my subtopics as a HTML fragment, using a template.

        Allows the template to be selected by subtopics_style folder
        property. If there is no property or the specified template
        does not exist, uses the built-in subtopics_outline.
        Some overlap with the show_subtopics property.
        """
        DEFAULTSTYLE = 'outline'
        style = getattr(self.folder(),'subtopics_style',None)
        if not (style and self.hasSkinTemplate('subtopics_'+style)):
            style = DEFAULTSTYLE
        return self.getSkinTemplate('subtopics_'+style)(self,REQUEST)

    security.declareProtected(Permissions.View, 'navlinks')
    def navlinks(self):
        """
        Return HTML for my next/previous/up links.
        """
        none = 'none'
        t = ''
        prev, next = self.previousPage(), self.nextPage()
        if prev: prev = self.renderLink('['+prev+']',access_key='P')
        else: prev = none
        if next: next = self.renderLink('['+next+']',access_key='N')
        else: next = none
        t += '<span class="accesskey">n</span>ext:&nbsp;%s <span class="accesskey">p</span>revious:&nbsp;%s' \
             % (next,prev) # Info style!
        t += ' <span class="accesskey">u</span>p:&nbsp;%s' \
             % ((self.parents and self.renderLink('['+self.primaryParentName()+']',
                                                  access_key='U'))
                or none)
        #if contents:
        #    contentsurl = self.contentsUrl()
        #    contentslink = \
        #      '<a href="%s" title="show wiki contents">contents</a>'\
        #      % (contentsurl)
        #    t += ' contents:&nbsp;%s' % contentslink
        return t

    security.declareProtected(Permissions.View, 'renderNesting')
    def renderNesting(self, nesting, here=None, enlarge_current=0,
                      suppress_hyperlink=0, suppress_current=0,
                      did=None, got=None, indent=''):
        """
        Format a nesting structure as HTML unordered lists of wiki links.

        - nesting is the nesting to be formatted
        - here is the page name to highlight with "you are here", if any
        - if enlarge_current is true, here will be enlarged instead
        - if suppress_hyperlink is true, here will not be linked
          (backwards compatibility for old editforms)
        - if suppress_current is true, here will not be shown at all
        - did, got & indent are for recursion, callers should not use

        """
        #XXX cleanup
        
        if suppress_current and nesting[0] == here: # a single childless page
            return ''
        if did is None: did = []
        if got is None:
            got = ['<ul class="aqtree3clickable">'] #SKWM 
            recursing = 0
        else:
            recursing = 1
        for n in nesting:
            if type(n) == ListType:
                if not (n[0]==here and suppress_current): #XXX temp
                    got.append('%s <li>[%s]' % (indent,n[0]))
                if len(n) > 1:
                    if not (n[0]==here and suppress_current): #XXX temp
                        got.append('<ul>')
                    for i in n[1:]:
                        if type(i) == ListType:
                            got = self.renderNesting(
                                [i],here,did=did,got=got,indent=indent+' ')
                        else:
                            got.append('%s <li>[%s]</li>' % (indent,i))
                    if not (n[0]==here and suppress_current): #XXX temp
                        got.append("</ul>")
                else:
                    got[-1] += ' ...' # a parent whose children were omitted
                if not (n[0]==here and suppress_current):
                    got.append('%s </li>' % indent)
            else:
                got.append('%s <li>[%s]</li>' % (indent,n))
        if recursing: return got

        # finish up, do pretty printing options and wiki links
        got.append("</ul>")
        t = join(got, "\n")
        if here:
            if enlarge_current:
                t = re.sub(r'(\[%s\])' % re.escape(here),
                           r'<big><big><big><big><strong>\1</strong></big></big></big></big>',
                           t)
                # XXX temporary kludge.. assume we are in the page header here
                t = re.sub(r'(\[%s\])' % re.escape(self.pageName()),
                           '<a href="%s/backlinks" title="%s">%s</a>' % (
                               self.page_url(),
                               _("which pages link to this one ?"),
                               self.formatWikiname(self.pageName())),
                           t)
            elif SHOW_CURRENT_PAGE_IN_CONTENTS:
                t = re.sub(r'(\[%s\])' % re.escape(here),
                           r'\1 <b><-- %s.</b>' % _("You are here"),
                           t)
            if suppress_hyperlink:
                t = re.sub(r'(\[%s\])' % re.escape(here), r'!\1', t)
        #t = self.renderLinksIn(t) # too expensive for now.. do it on the cheap
        wikiurl = self.wiki_url()
        def quicklink(match):
            page = match.group(1)
            return '<a href="%s/%s" name="%s">%s</a>' \
                   % (wikiurl,self.canonicalIdFrom(page),quote(page),
                      self.formatWikiname(page))
        t = re.sub(bracketedexpr,quicklink,t)
        return t

    security.declareProtected(Permissions.View, 'renderNestingX')
    def renderNestingX(self, nesting, here=None, suppress_current=0,
                      did=None, got=None, indent=''):
        """
        Unpack a nesting structure into a list.

        Like renderNesting, but supports contextX which allows a skin
        template to do the rendering.
        """
        if suppress_current and nesting[0] == here: # a single childless page
            return []
        if did is None: did = []
        if got is None:
            got = [ {'type':'+'} ]
            recursing = 0
        else:
            recursing = 1
        for n in nesting:
            if type(n) == ListType:
                if not (n[0]==here and suppress_current): #XXX temp
                    t = (n[0]==here and '=!' or '=')
                    got.append( {'type':t, 'page':str(n[0])} )
                if len(n) > 1:
                    if not (n[0]==here and suppress_current): #XXX temp
                        got.append( {'type':'+'} )
                    for i in n[1:]:
                        if type(i) == ListType:
                            got = self.renderNestingX(
                                [i],here,did=did,got=got,indent=indent+' ')
                        else:
                            t = (i==here and '=!' or '=')
                            got.append( {'type':t, 'page':str(i)} )
                    if not (n[0]==here and suppress_current): #XXX temp
                        got.append( {'type':'-'} )
                else:
                    
                    got[-1]['type'] += '.' # a parent whose children were omitted
            else:
                t = (n==here and '=!' or '=')
                got.append( {'type':t, 'page':str(n)} )
        if recursing: return got

        # finish up, do pretty printing options and wiki links
        got.append( {'type':'-'} )

        wikiurl = self.wiki_url()
        for g in got :
            if '=' in g['type'] :
                g['href'] = wikiurl + '/' + self.canonicalIdFrom(g['page'])
                g['name'] = quote(g['page'])
                
        return got

    # backwards compatibility
    map = contents

InitializeClass(OutlineRenderingMixin)

class OutlineSupport(
    OutlineManagerMixin, 
    ParentsPropertyMixin,
    SubtopicsPropertyMixin,
    OutlineRenderingMixin
    ):
    """
    I make a page aware of it's place within the overall wiki outline
    (page hierarchy).
    """
    pass
